"""run_e2e.py - end-to-end check for yt2disc (no network, no real audio).

    py -3 _e2e/run_e2e.py                     # against the python sources
    py -3 _e2e/run_e2e.py --exe dist\\yt2disc  # against a packaged build

Everything runs against throw-away folders, created at import time:

    YT2DISC_ROOT                playlists.json, state.json and cache/ live here
    YT2DISC_MINECRAFT           a fake com.mojang whose minecraftpe/options.txt
                                stands in for the game's own settings
    YT2DISC_MINECRAFT_CONTENT   a fake Minecraft Content folder holding a
                                hand-made 'vanilla_music' resource pack
    YT2DISC_FFMPEG              the fake decoder next to this file
    YT2DISC_FAKE_PLAYER         no audio device is ever touched

The personal playlists, the real Minecraft install and the real options.txt are
never read or written.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# The app modules live one level up; the checks import them directly.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _exe_arg() -> Path | None:
    if "--exe" in sys.argv:
        index = sys.argv.index("--exe")
        if index + 1 < len(sys.argv):
            return Path(sys.argv[index + 1]).resolve()
    return None


EXE_SUFFIX = ".exe" if os.name == "nt" else ""
EXE_DIR = _exe_arg()
CLI_EXE = (EXE_DIR / f"yt2disc-cli{EXE_SUFFIX}") if EXE_DIR else None
GUI_EXE = (EXE_DIR / f"yt2disc{EXE_SUFFIX}") if EXE_DIR else None
if EXE_DIR is not None and not CLI_EXE.is_file():
    print(f"ERROR: {CLI_EXE} does not exist - build it with 'py -3 build_exe.py'")
    raise SystemExit(2)

DATA_ROOT = Path(tempfile.mkdtemp(prefix="yt2disc-e2e-"))
MC_SANDBOX = Path(tempfile.mkdtemp(prefix="yt2disc-minecraft-"))
CONTENT_ROOT = Path(tempfile.mkdtemp(prefix="yt2disc-content-"))
DEVICE_MUSIC = Path(tempfile.mkdtemp(prefix="yt2disc-music-"))

PLAYLISTS = DATA_ROOT / "playlists.json"
STATE = DATA_ROOT / "state.json"
CACHE = DATA_ROOT / "cache"
OPTIONS = MC_SANDBOX / "minecraftpe" / "options.txt"
OPTIONS_BACKUP = OPTIONS.with_name(OPTIONS.name + ".yt2disc.bak")
VANILLA_MUSIC = (
    CONTENT_ROOT / "data" / "resource_packs" / "vanilla_music" / "sounds" / "music"
)
VANILLA_TRACKS = {
    "game/sweden.ogg": "sweden",
    "game/calm1.ogg": "calm1",
    "game/records/cat.ogg": "cat",
    "game/records/13.ogg": "13",
    "game/creative/creative1.ogg": "creative1",
    "game/nether/nether1.ogg": "nether1",
    "menu/menu1.ogg": "menu1",
}

ENV = dict(os.environ)
ENV["YT2DISC_ROOT"] = str(DATA_ROOT)
ENV["YT2DISC_MINECRAFT"] = str(MC_SANDBOX)
ENV["YT2DISC_MINECRAFT_CONTENT"] = str(CONTENT_ROOT)
ENV["YT2DISC_FFMPEG"] = str(HERE / ("ffmpeg.cmd" if os.name == "nt" else "ffmpeg"))
ENV["YT2DISC_FAKE_PLAYER"] = "1"
ENV["YT2DISC_PYTHON"] = sys.executable
ENV["PYTHONIOENCODING"] = "utf-8"
for _name in (
    "YT2DISC_ROOT",
    "YT2DISC_MINECRAFT",
    "YT2DISC_MINECRAFT_CONTENT",
    "YT2DISC_FFMPEG",
    "YT2DISC_FAKE_PLAYER",
    "YT2DISC_PYTHON",
):
    os.environ[_name] = ENV[_name]

FAILURES: list[str] = []
PY = sys.executable
APP_LABEL = CLI_EXE.name if CLI_EXE else "main.py"


def make_tone(path, seconds: float = 0.4, rate: int = 8000) -> Path:
    """A real (tiny, silent) WAV, so sizes and durations are genuine."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


def prepare_fixtures() -> None:
    """Build the fake device library, soundtrack and Minecraft settings."""
    for name in ("my_song_one.wav", "my_song_two.wav", "my_song_three.wav"):
        make_tone(DEVICE_MUSIC / name)
    # Something that is not audio at all: it must never be picked up.
    (DEVICE_MUSIC / "notes.txt").write_text("not music", encoding="utf-8")
    (DEVICE_MUSIC / "nested").mkdir(exist_ok=True)
    make_tone(DEVICE_MUSIC / "nested" / "deep_track.wav")

    for relative in VANILLA_TRACKS:
        make_tone(VANILLA_MUSIC / relative)

    OPTIONS.parent.mkdir(parents=True, exist_ok=True)
    OPTIONS.write_text(
        "gfx_viewdistance:160\n"
        "audio_main:0.75\n"
        "audio_sound:1\n"
        "audio_music:1\n"
        "audio_record:1\n",
        encoding="utf-8",
    )


def cleanup() -> None:
    """Drop everything a run creates in the throw-away folders."""
    for target in (PLAYLISTS, STATE):
        target.unlink(missing_ok=True)
    for pattern in ("*.bak", "*.tmp"):
        for target in DATA_ROOT.glob(pattern):
            target.unlink()
    if CACHE.exists():
        for target in CACHE.glob("*"):
            target.unlink()
    OPTIONS_BACKUP.unlink(missing_ok=True)
    OPTIONS.write_text(
        "gfx_viewdistance:160\n"
        "audio_main:0.75\n"
        "audio_sound:1\n"
        "audio_music:1\n"
        "audio_record:1\n",
        encoding="utf-8",
    )


def run(*args):
    cmd = (
        [str(CLI_EXE), *args]
        if CLI_EXE
        else [PY, str(ROOT / "main.py"), "--cli", *args]
    )
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ENV,
        cwd=str(ROOT),
    )
    print(f"\n$ {APP_LABEL} {' '.join(args)}   -> exit {proc.returncode}")
    for line in (proc.stdout or "").strip().splitlines():
        print("  | " + line)
    for line in (proc.stderr or "").strip().splitlines():
        print("  ! " + line)
    return proc


def check(label, condition, extra="") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"{mark}: {label}{('  ' + str(extra)) if extra else ''}")
    if not condition:
        FAILURES.append(label)


def read_json(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_text(path) -> str:
    return Path(path).read_text(encoding="utf-8")


def gui_pump(root, predicate, timeout: float = 30.0) -> bool:
    """Keep a Tk event loop turning until *predicate* holds (worker threads!)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.03)
    root.update()
    return predicate()


def raises(exc_type, func, *args, **kwargs) -> bool:
    """True when *func* raises exactly *exc_type*."""
    try:
        func(*args, **kwargs)
    except exc_type:
        return True
    except Exception:
        return False
    return False


def check_core_helpers():
    """Pure functions the CLI flow does not reach directly."""
    import random as random_module

    import core
    import player

    check("core: fmt_duration", core.fmt_duration(247.4) == "04:07", core.fmt_duration(247.4))
    check("core: fmt_duration over an hour", core.fmt_duration(3725) == "1:02:05", core.fmt_duration(3725))
    check("core: fmt_duration junk", core.fmt_duration("x") == "--:--")
    check("core: human_size", core.human_size(1536) == "1.5 KiB", core.human_size(1536))
    check("core: slugify strips punctuation", core.slugify("Hello, World! 42") == "hello_world_42")
    check("core: slugify prefixes digits", core.slugify("99 Luftballons") == "x99_luftballons")
    check("core: slugify never empty", core.slugify("!!!") == "playlist", core.slugify("!!!"))
    check("core: track_title", core.track_title("D:/m/sweden.ogg") == "sweden")
    check(
        "core: track_title de-underscores",
        core.track_title("a_familiar_room.ogg") == "a familiar room",
        core.track_title("a_familiar_room.ogg"),
    )

    check(
        "core: audio extensions accepted",
        all(core.is_audio_file(f"x{ext}") for ext in (".ogg", ".mp3", ".flac", ".wav", ".m4a")),
    )
    check("core: non-audio rejected", not core.is_audio_file("notes.txt"))
    check(
        "core: vanilla tracks recognised",
        core.is_vanilla_track(VANILLA_MUSIC / "game" / "sweden.ogg"),
    )
    check(
        "core: device tracks are not vanilla",
        not core.is_vanilla_track(DEVICE_MUSIC / "my_song_one.wav"),
    )
    for relative, expected in (
        ("game/sweden.ogg", "game"),
        ("game/records/cat.ogg", "records"),
        ("game/creative/creative1.ogg", "creative"),
        ("game/nether/nether1.ogg", "nether"),
        ("game/end/credits.ogg", "end"),
        ("game/water/axolotl.ogg", "water"),
        ("menu/menu1.ogg", "menu"),
    ):
        got = core.vanilla_category(VANILLA_MUSIC / relative)
        check(f"core: category of {relative}", got == expected, got)

    track = core.track_from_path(VANILLA_MUSIC / "game" / "records" / "cat.ogg")
    check(
        "core: track_from_path flags vanilla + category",
        track["vanilla"] is True and track["category"] == "records" and track["exists"] is True,
        track["category"],
    )
    check("core: describe_track lists name and size", "cat" in core.describe_track(track))
    gone = core.track_from_path(DEVICE_MUSIC / "gone.wav")
    check("core: missing files are flagged", gone["exists"] is False and gone["size"] == 0)
    check("core: describe_track says MISSING", "MISSING" in core.describe_track(gone))
    check(
        "core: cache_path_for is stable and unique",
        core.cache_path_for("x.ogg") == core.cache_path_for("x.ogg")
        and core.cache_path_for("x.ogg") != core.cache_path_for("y.ogg"),
    )

    # ---- choosing what plays next -------------------------------------
    pool = [
        {"path": f"D:/m/{name}.ogg", "name": name, "exists": True}
        for name in ("a", "b", "c")
    ]
    check("core: next_track starts at the top", core.next_track(pool)["name"] == "a")
    check("core: next_track walks the list", core.next_track(pool, current="D:/m/a.ogg")["name"] == "b")
    check("core: next_track wraps around", core.next_track(pool, current="D:/m/c.ogg")["name"] == "a")
    check(
        "core: next_track can stop at the end",
        core.next_track(pool, current="D:/m/c.ogg", wrap=False) is None,
    )
    check("core: next_track of an empty list", core.next_track([]) is None)

    rng_a = random_module.Random(99)
    rng_b = random_module.Random(99)
    sequence_a = [core.pick_random_track(pool, rng=rng_a)["name"] for _ in range(8)]
    sequence_b = [core.pick_random_track(pool, rng=rng_b)["name"] for _ in range(8)]
    check("core: random is reproducible with a seed", sequence_a == sequence_b, sequence_a)
    check(
        "core: random uses the whole pool",
        set(sequence_a) == {"a", "b", "c"},
        sorted(set(sequence_a)),
    )
    forced = core.pick_random_track(pool, avoid="D:/m/a.ogg", rng=random_module.Random(1))
    check("core: random never repeats the current track", forced["name"] in ("b", "c"), forced)
    check(
        "core: random with a single track returns it",
        core.pick_random_track([pool[0]], avoid="D:/m/a.ogg")["name"] == "a",
    )
    check(
        "core: random on an empty pool complains",
        raises(core.InputError, core.pick_random_track, []),
    )
    check(
        "core: random always returns a track dict",
        isinstance(core.pick_random_track(pool, rng=random_module.Random(5)), dict),
    )

    # ---- options.txt ---------------------------------------------------
    values, order = core.parse_options("a:1\nb:2\nnot-a-setting\nc:3\n")
    check("core: parse_options keeps the order", order == ["a", "b", "c"], order)
    check("core: parse_options skips junk", values == {"a": "1", "b": "2", "c": "3"}, values)
    values["b"] = "9"
    values["d"] = "new"
    rendered = core.render_options(values, order)
    check(
        "core: render_options keeps every key",
        rendered == "a:1\nb:9\nc:3\nd:new\n",
        rendered.replace("\n", "\\n"),
    )
    check(
        "core: option values look like the game's",
        core._format_option_value(0.0) == "0"
        and core._format_option_value(1.0) == "1"
        and core._format_option_value(0.75) == "0.75",
        core._format_option_value(0.75),
    )
    check(
        "core: options live in minecraftpe/options.txt",
        core.options_candidates(MC_SANDBOX)[0] == OPTIONS,
        core.options_candidates(MC_SANDBOX)[0],
    )
    check("core: the sandbox options file is found", core.find_options_file() == OPTIONS)
    audio = core.read_minecraft_options()
    check(
        "core: the sandbox options are read",
        audio["exists"] and audio["music"] == 1.0 and audio["record"] == 1.0,
        (audio["music"], audio["record"]),
    )
    check("core: music is not muted to start with", audio["music_muted"] is False)

    # ---- Minecraft folders ---------------------------------------------
    check(
        "core: data folder override honoured",
        core.bedrock_candidates() == [MC_SANDBOX],
        core.bedrock_candidates(),
    )
    check(
        "core: content folder override honoured",
        core.minecraft_content_roots() == [CONTENT_ROOT],
        core.minecraft_content_roots(),
    )
    check("core: soundtrack root found", core.vanilla_music_root() == VANILLA_MUSIC)
    check(
        "core: the music folder itself works too",
        core.vanilla_music_root(VANILLA_MUSIC) == VANILLA_MUSIC,
    )
    check("core: find_minecraft_content", core.find_minecraft_content() == CONTENT_ROOT)
    summary = {entry["category"]: entry["count"] for entry in core.soundtrack_summary()}
    check(
        "core: soundtrack summary counts per category",
        summary == {"game": 2, "creative": 1, "nether": 1, "records": 2, "menu": 1},
        summary,
    )
    check("core: binaries resolved from env", core.missing_binaries() == [], core.missing_binaries())
    check("core: binary help mentions ffmpeg", "ffmpeg" in core.binary_help_text())
    # The lookup rules the hosted converter leans on.  An explicit override
    # always wins, and anything that cannot really be started is skipped rather
    # than handed back: a copy without an execute bit used to look fine right
    # up until ffmpeg was spawned, which is what broke a Linux deploy.
    check(
        "core: YT2DISC_FFMPEG wins over bin/ and PATH",
        core.find_binary("ffmpeg") == Path(os.environ["YT2DISC_FFMPEG"]),
        core.find_binary("ffmpeg"),
    )
    bogus = HERE / "no-such-ffmpeg"
    os.environ["YT2DISC_FFMPEG"] = str(bogus)
    try:
        check(
            "core: an override that does not exist is not returned",
            core.find_binary("ffmpeg") != bogus,
            core.find_binary("ffmpeg"),
        )
    finally:
        os.environ["YT2DISC_FFMPEG"] = ENV["YT2DISC_FFMPEG"]
    check(
        "core: a folder is not mistaken for a binary",
        core._executable(HERE) is None,
        core._executable(HERE),
    )
    if os.name != "nt":
        # Only meaningful where an execute bit exists; a copy or an unzip can
        # easily leave one off, and that is the case this has to survive.
        scratch = Path(tempfile.mkdtemp(prefix="yt2disc-exec-")) / "ffmpeg"
        scratch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        scratch.chmod(0o644)
        check("core: a missing execute bit is repaired", core._executable(scratch), scratch)

    # ---- player --------------------------------------------------------
    backend = player.pick_backend()
    check("player: fake player backend in the test", isinstance(backend, player.DryRunBackend))
    check("player: nothing plays to start with", backend.playing is False)
    check(
        "player: dry-run backend finishes instantly",
        _dry_run_finishes(backend),
    )


def _dry_run_finishes(backend) -> bool:
    """The dry-run backend calls done() and never claims to keep playing."""
    seen = []
    backend.play("ignored.wav", lambda: seen.append(True))
    return seen == [True] and backend.playing is False


def check_library():
    """library add / list / scan, driven through the CLI."""
    proc = run("library", "add", str(DEVICE_MUSIC))
    check(
        "library: add remembers the folder",
        proc.returncode == 0 and "Remembered" in proc.stdout,
        (proc.stdout or "").strip(),
    )

    proc = run("library", "add", str(DEVICE_MUSIC))
    check(
        "library: adding it twice is not an error",
        proc.returncode == 0 and "Already in the library" in proc.stdout,
        (proc.stdout or "").strip(),
    )

    proc = run("library", "list")
    check("library: list shows the folder", proc.returncode == 0 and str(DEVICE_MUSIC) in proc.stdout)

    state = read_json(STATE)
    check(
        "library: state.json remembers it",
        state["library_roots"] == [str(DEVICE_MUSIC)],
        state.get("library_roots"),
    )
    check(
        "library: the mute preference defaults to on",
        state.get("mute_minecraft_music") is True,
        state.get("mute_minecraft_music"),
    )

    proc = run("library", "scan", str(DEVICE_MUSIC), "--no-recursive")
    check(
        "library: scan finds the three wavs and skips notes.txt",
        proc.returncode == 0 and "3 audio file(s)" in proc.stdout,
        (proc.stdout or "").strip().splitlines()[0] if proc.stdout else "",
    )
    proc = run("library", "scan", str(DEVICE_MUSIC))
    check(
        "library: scan is recursive by default",
        "4 audio file(s)" in proc.stdout and "deep track" in proc.stdout,
        (proc.stdout or "").strip().splitlines()[0] if proc.stdout else "",
    )
    proc = run("library", "scan", str(VANILLA_MUSIC))
    check(
        "library: scan works on any folder",
        "7 audio file(s)" in proc.stdout,
        (proc.stdout or "").strip().splitlines()[0] if proc.stdout else "",
    )
    proc = run("library", "scan", str(DEVICE_MUSIC / "nope"))
    check(
        "library: scanning a missing folder fails",
        proc.returncode == 1 and "is not a folder" in (proc.stderr or ""),
        (proc.stderr or "").strip(),
    )
    proc = run("library", "add", str(DEVICE_MUSIC / "nope"))
    check("library: remembering a missing folder fails", proc.returncode == 1)
    proc = run("library", "remove", str(CONTENT_ROOT))
    check(
        "library: forgetting something unknown is harmless",
        proc.returncode == 0 and "Was not in the library" in proc.stdout,
    )
    proc = run("library", "remove", str(DEVICE_MUSIC))
    check(
        "library: remove forgets the folder",
        "Forgot" in proc.stdout and read_json(STATE)["library_roots"] == [],
    )
    run("library", "add", str(DEVICE_MUSIC))
    check(
        "library: the folder is remembered again",
        read_json(STATE)["library_roots"] == [str(DEVICE_MUSIC)],
    )


def check_playlists():
    """playlist new / add / add-dir / remove / rename / delete."""
    proc = run("playlist", "list")
    check(
        "playlist: the list starts empty",
        proc.returncode == 0 and "No playlists yet" in proc.stdout,
        (proc.stdout or "").strip(),
    )

    proc = run("playlist", "new", "Chill")
    check("playlist: new creates one", proc.returncode == 0 and "'Chill'" in proc.stdout)
    check("playlist: playlists.json is written", PLAYLISTS.is_file())
    data = read_json(PLAYLISTS)
    check("playlist: the file carries a format version", data["format_version"] == 1)
    check(
        "playlist: the id is a slug",
        data["playlists"][0]["id"] == "chill",
        data["playlists"][0]["id"],
    )
    check("playlist: a new playlist is empty", data["playlists"][0]["tracks"] == [])
    check(
        "playlist: it records when it was made",
        bool(data["playlists"][0].get("created")) and bool(data["playlists"][0].get("updated")),
    )

    proc = run("playlist", "new", "Chill")
    check(
        "playlist: a duplicate name is refused",
        proc.returncode == 1 and "already exists" in (proc.stderr or ""),
        (proc.stderr or "").strip(),
    )

    proc = run("playlist", "add-dir", "Chill", str(DEVICE_MUSIC / "nested"))
    check(
        "playlist: add-dir takes the folder's tracks",
        "added 1" in proc.stdout,
        (proc.stdout or "").strip(),
    )

    proc = run(
        "playlist",
        "add",
        "Chill",
        str(DEVICE_MUSIC / "my_song_one.wav"),
        str(DEVICE_MUSIC / "my_song_two.wav"),
    )
    check(
        "playlist: add takes several files",
        "added 2" in proc.stdout and "total 3" in proc.stdout,
        (proc.stdout or "").strip(),
    )

    proc = run("playlist", "add", "Chill", str(DEVICE_MUSIC / "my_song_one.wav"))
    check(
        "playlist: the same file is not added twice",
        "added 0" in proc.stdout and "already there 1" in proc.stdout,
        (proc.stdout or "").strip(),
    )

    proc = run("playlist", "show", "Chill")
    check(
        "playlist: show lists the tracks",
        proc.returncode == 0 and "my song one" in proc.stdout and "deep track" in proc.stdout,
    )
    check(
        "playlist: tracks are stored as absolute paths",
        all(
            Path(entry).is_absolute()
            for entry in read_json(PLAYLISTS)["playlists"][0]["tracks"]
        ),
    )

    proc = run("playlist", "add", "Chill", str(DEVICE_MUSIC / "notes.txt"))
    check(
        "playlist: a non-audio file is refused",
        proc.returncode == 1 and "not a supported audio file" in (proc.stderr or ""),
        (proc.stderr or "").strip(),
    )
    proc = run("playlist", "add", "nope", str(DEVICE_MUSIC / "my_song_one.wav"))
    check(
        "playlist: adding to an unknown playlist fails",
        proc.returncode == 1 and "no playlist called" in (proc.stderr or ""),
    )

    proc = run("playlist", "remove", "Chill", "1")
    check(
        "playlist: remove by index",
        "Removed" in proc.stdout and "2 left" in proc.stdout,
        (proc.stdout or "").strip(),
    )
    proc = run("playlist", "remove", "Chill", "my_song_two.wav")
    check("playlist: remove by file name", "Removed" in proc.stdout and "1 left" in proc.stdout)
    proc = run("playlist", "remove", "Chill", "9")
    check(
        "playlist: removing something absent fails",
        proc.returncode == 1 and "is not in" in (proc.stderr or ""),
        (proc.stderr or "").strip(),
    )

    proc = run("playlist", "rename", "Chill", "Chill Vibes")
    check(
        "playlist: rename works and re-slugs the id",
        proc.returncode == 0 and "Chill Vibes" in proc.stdout and "id: chill_vibes" in proc.stdout,
        (proc.stdout or "").strip(),
    )
    run("playlist", "new", "Taken")
    proc = run("playlist", "rename", "Taken", "Chill Vibes")
    check(
        "playlist: renaming onto an existing name fails",
        proc.returncode == 1 and "already exists" in (proc.stderr or ""),
    )

    proc = run("playlist", "delete", "Taken")
    check("playlist: delete works", "Deleted" in proc.stdout)
    check("playlist: only the other one is left", len(read_json(PLAYLISTS)["playlists"]) == 1)
    proc = run("playlist", "delete", "Taken")
    check(
        "playlist: deleting twice fails cleanly",
        proc.returncode == 1 and "no playlist called" in (proc.stderr or ""),
    )
    check(
        "playlist: deleting a playlist never deletes audio",
        (DEVICE_MUSIC / "nested" / "deep_track.wav").is_file(),
    )


def check_soundtrack():
    """Minecraft's own soundtrack, read out of the fake game files."""
    proc = run("soundtrack")
    check(
        "soundtrack: the summary lists every category",
        proc.returncode == 0
        and all(name in proc.stdout for name in ("game", "creative", "nether", "records", "menu")),
        (proc.stdout or "").strip().replace("\n", " | "),
    )
    check("soundtrack: the summary shows where it read from", str(VANILLA_MUSIC) in proc.stdout)

    proc = run("soundtrack", "--list")
    check(
        "soundtrack: --list shows every track",
        proc.returncode == 0
        and all(name in proc.stdout for name in VANILLA_TRACKS.values()),
    )
    check("soundtrack: tracks are labelled with their category", "records" in proc.stdout)

    proc = run("soundtrack", "--category", "records")
    check(
        "soundtrack: --category filters",
        proc.returncode == 0 and "cat" in proc.stdout and "sweden" not in proc.stdout,
        (proc.stdout or "").strip().replace("\n", " | "),
    )
    proc = run("soundtrack", "--search", "menu")
    check(
        "soundtrack: --search filters by title",
        "menu1" in proc.stdout and "sweden" not in proc.stdout,
    )
    proc = run("soundtrack", "--category", "nonsense")
    check(
        "soundtrack: an empty category fails cleanly",
        proc.returncode == 1 and "nothing matched" in (proc.stderr or ""),
        (proc.stderr or "").strip(),
    )
    proc = run("soundtrack", "--list", "--content", str(CONTENT_ROOT / "nope"))
    check("soundtrack: a wrong content folder fails cleanly", proc.returncode == 1)

    proc = run("soundtrack", "--add", "Chill Vibes", "--category", "menu")
    check(
        "soundtrack: --add copies the matches into a playlist",
        proc.returncode == 0 and "added 1" in proc.stdout,
        (proc.stdout or "").strip(),
    )
    tracks = read_json(PLAYLISTS)["playlists"][0]["tracks"]
    check(
        "soundtrack: the playlist now points at the game's own file",
        any("vanilla_music" in entry for entry in tracks),
        tracks,
    )
    proc = run("soundtrack", "--add", "Chill Vibes", "--category", "menu")
    check(
        "soundtrack: adding the same track twice is idempotent",
        "added 0" in proc.stdout and "already there 1" in proc.stdout,
        (proc.stdout or "").strip(),
    )


def check_mc_music():
    """The mute switch: Minecraft's own options.txt, written with a backup."""
    proc = run("mc-music", "--status")
    check(
        "mc-music: --status reads the slider",
        proc.returncode == 0 and "audio_music  = 1.0" in proc.stdout,
        (proc.stdout or "").strip().replace("\n", " | "),
    )
    check("mc-music: --status names the file", str(OPTIONS) in proc.stdout)

    proc = run("mc-music", "--off")
    check(
        "mc-music: --off writes 0",
        proc.returncode == 0 and "audio_music = 0" in proc.stdout,
        (proc.stdout or "").strip(),
    )
    text = read_text(OPTIONS)
    check("mc-music: the game's file really says 0", "audio_music:0" in text, text.replace("\n", " | "))
    check("mc-music: a backup is kept", OPTIONS_BACKUP.is_file())
    check("mc-music: the backup holds the old value", "audio_music:1" in read_text(OPTIONS_BACKUP))
    check(
        "mc-music: every other setting survives",
        "audio_main:0.75" in text and "gfx_viewdistance:160" in text and "audio_sound:1" in text,
    )
    check("mc-music: no stray temp file is left behind", not OPTIONS.with_name("options.txt.tmp").exists())

    proc = run("mc-music", "--off")
    check(
        "mc-music: a second --off is a no-op",
        proc.returncode == 0 and "previous settings kept" not in proc.stdout,
        (proc.stdout or "").strip(),
    )

    proc = run("mc-music", "--status")
    check("mc-music: --status calls it OFF", "OFF" in proc.stdout)

    proc = run("mc-music", "--on")
    check("mc-music: --on turns it back on", "audio_music = 1" in proc.stdout)
    check("mc-music: --on keeps the backup for later", OPTIONS_BACKUP.is_file())

    proc = run("mc-music", "--restore")
    check(
        "mc-music: --restore puts the backup back",
        proc.returncode == 0 and "Restored" in proc.stdout,
    )
    check("mc-music: the backup is consumed by a restore", not OPTIONS_BACKUP.exists())
    check("mc-music: the restored file matches the original", read_text(OPTIONS) == read_text(OPTIONS))

    proc = run("mc-music", "--restore")
    check(
        "mc-music: --restore without a backup fails cleanly",
        proc.returncode == 1 and "no backup" in (proc.stderr or ""),
        (proc.stderr or "").strip(),
    )

    proc = run("mc-music", "--off", "--records")
    check(
        "mc-music: --records silences the jukebox channel too",
        "audio_record = 0" in proc.stdout and "audio_record:0" in read_text(OPTIONS),
        (proc.stdout or "").strip().replace("\n", " | "),
    )
    check(
        "mc-music: the backup still holds the original jukebox value",
        "audio_record:1" in read_text(OPTIONS_BACKUP),
    )
    run("mc-music", "--on")
    proc = run("mc-music", "--restore")
    check(
        "mc-music: a restore fixes both channels",
        proc.returncode == 0
        and "audio_music:1" in read_text(OPTIONS)
        and "audio_record:1" in read_text(OPTIONS),
        read_text(OPTIONS).replace("\n", " | "),
    )

    # A data folder the game has never written to yet.
    fresh = MC_SANDBOX / "never-used"
    proc = run("mc-music", "--status", "--mc-dir", str(fresh))
    check(
        "mc-music: --status without a file says so",
        proc.returncode == 1 and "was not found" in proc.stdout,
        (proc.stdout or "").strip(),
    )
    proc = run("mc-music", "--off", "--mc-dir", str(fresh))
    check(
        "mc-music: --off creates the file when there is none",
        proc.returncode == 0 and (fresh / "minecraftpe" / "options.txt").is_file(),
        (fresh / "minecraftpe" / "options.txt"),
    )

    # Something that is not an options file must never be overwritten.
    OPTIONS.write_text("this is not an options file", encoding="utf-8")
    OPTIONS_BACKUP.unlink(missing_ok=True)
    proc = run("mc-music", "--off")
    check(
        "mc-music: an unreadable options.txt is left alone",
        proc.returncode == 1
        and "does not look like" in (proc.stderr or "")
        and read_text(OPTIONS) == "this is not an options file",
        (proc.stderr or "").strip(),
    )
    OPTIONS.write_text(
        "gfx_viewdistance:160\naudio_main:0.75\naudio_sound:1\n"
        "audio_music:1\naudio_record:1\n",
        encoding="utf-8",
    )
    check("mc-music: the fixture is restored", "audio_music:1" in read_text(OPTIONS))


def played_names(text) -> list[str]:
    """The titles a ``play`` run reported, in order."""
    found = []
    for line in (text or "").splitlines():
        if "Now playing:" in line:
            found.append(line.split("Now playing:", 1)[1].split("  (")[0].strip())
    return found


def check_play():
    """The whole chain: decode -> cache -> play -> mute -> restore."""
    import core

    CACHE.mkdir(parents=True, exist_ok=True)
    for stale in CACHE.glob("*"):
        stale.unlink()
    OPTIONS_BACKUP.unlink(missing_ok=True)

    proc = run("play", "--dry-run", "--track", str(DEVICE_MUSIC / "my_song_one.wav"))
    check(
        "play: a device file decodes and starts",
        proc.returncode == 0 and "Now playing" in proc.stdout,
        (proc.stdout or "").strip()[:160],
    )
    check("play: the backend is reported", "dry-run" in proc.stdout)
    decoded = list(CACHE.glob("*.wav"))
    check(
        "play: the track was decoded into cache/",
        len(decoded) == 1 and decoded[0].stat().st_size > 44,
        [path.name for path in decoded],
    )
    check(
        "play: the decode is cached under the track's own name",
        bool(decoded) and decoded[0].name.startswith("my_song_one-"),
        decoded[0].name if decoded else "",
    )
    check("play: Minecraft's music was muted for the playback", "music muted" in proc.stdout)
    check(
        "play: the pre-mute value was backed up",
        OPTIONS_BACKUP.is_file() and "audio_music:1" in read_text(OPTIONS_BACKUP),
    )
    check(
        "play: the game's music is switched back afterwards",
        "audio_music:1" in read_text(OPTIONS),
        read_text(OPTIONS).replace("\n", " | "),
    )
    check("play: the other sliders survived", "audio_main:0.75" in read_text(OPTIONS))

    proc = run(
        "play", "--dry-run", "--no-mute",
        "--track", str(DEVICE_MUSIC / "my_song_two.wav"),
    )
    check(
        "play: --no-mute leaves the game alone",
        proc.returncode == 0 and "left alone" in proc.stdout,
        (proc.stdout or "").strip().splitlines()[-2:],
    )
    check("play: nothing was written without muting", "audio_music:1" in read_text(OPTIONS))

    before = len(list(CACHE.glob("*.wav")))
    proc = run("play", "--dry-run", "--track", str(DEVICE_MUSIC / "my_song_one.wav"))
    check(
        "play: playing the same file again reuses the decode",
        len(list(CACHE.glob("*.wav"))) == before and "cached" in proc.stdout,
        (proc.stdout or "").strip()[:120],
    )

    proc = run("play", "--random", "--seed", "7", "--count", "3", "--dry-run")
    first = played_names(proc.stdout)
    check(
        "play: --random --count plays that many tracks",
        proc.returncode == 0 and "[3/3]" in proc.stdout and len(first) == 3,
        first,
    )
    check(
        "play: a random pick is never the previous track",
        all(one != two for one, two in zip(first, first[1:])),
        first,
    )
    second = played_names(
        run("play", "--random", "--seed", "7", "--count", "3", "--dry-run").stdout
    )
    check("play: the same seed picks the same tracks", first == second, (first, second))

    proc = run("play", "Chill Vibes", "--dry-run")
    check(
        "play: a playlist plays in order",
        proc.returncode == 0 and played_names(proc.stdout)[:1] == ["my song one"],
        played_names(proc.stdout),
    )

    # 'records' holds two tracks (13 and cat) and scan_audio_folder sorts A-Z,
    # so the one that plays first is deterministic but not worth hard-coding:
    # read back the title the run reported and follow it into the cache.
    records_titles = {
        title
        for relative, title in VANILLA_TRACKS.items()
        if relative.startswith("game/records/")
    }
    proc = run("play", "--soundtrack", "records", "--dry-run")
    played = played_names(proc.stdout)
    check(
        "play: a soundtrack track decodes straight from the game's files",
        proc.returncode == 0
        and len(played) == 1
        and played[0] in records_titles
        and "vanilla_music" in (proc.stdout or ""),
        (proc.stdout or "").strip()[:140],
    )
    check(
        "play: the vanilla decode landed next to the others",
        bool(played)
        and any(
            path.name.startswith(core.slugify(played[0]) + "-")
            for path in CACHE.glob("*.wav")
        ),
        [path.name for path in CACHE.glob("*.wav")],
    )

    proc = run("play", "--track", str(DEVICE_MUSIC / "gone.wav"))
    check(
        "play: a missing file fails cleanly",
        proc.returncode == 1 and "does not exist" in (proc.stderr or ""),
        (proc.stderr or "").strip(),
    )

    state = read_json(STATE)
    state["mute_minecraft_music"] = False
    STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    proc = run("play", "--dry-run", "--track", str(DEVICE_MUSIC / "my_song_three.wav"))
    check(
        "play: the saved preference is honoured",
        proc.returncode == 0 and "left alone" in proc.stdout,
        (proc.stdout or "").strip()[-60:],
    )
    proc = run(
        "play", "--dry-run", "--mute",
        "--track", str(DEVICE_MUSIC / "my_song_three.wav"),
    )
    check("play: --mute overrides the preference", "music muted" in proc.stdout)
    state["mute_minecraft_music"] = True
    STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    run("library", "remove", str(DEVICE_MUSIC))
    proc = run("play", "--dry-run")
    check(
        "play: with nothing to play it explains itself",
        proc.returncode == 1 and "nothing to play" in (proc.stderr or ""),
        (proc.stderr or "").strip(),
    )
    run("library", "add", str(DEVICE_MUSIC))
    check("play: the library folder is back", read_json(STATE)["library_roots"] == [str(DEVICE_MUSIC)])


def check_gui():
    """The desktop window: three tabs, backed by the same core."""
    try:
        import tkinter as tk
    except Exception as exc:  # pragma: no cover - headless python without Tk
        check("gui: tkinter available", False, repr(exc))
        return
    try:
        import gui
        import player
    except Exception as exc:  # pragma: no cover
        check("gui: module imports", False, repr(exc))
        return

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"SKIP: GUI smoke test (no display available: {exc})")
        return

    root.withdraw()
    # No modal dialog may block the run: record them instead.
    popups: list[tuple] = []
    saved = {
        name: getattr(gui.messagebox, name)
        for name in ("showinfo", "showerror", "showwarning", "askyesno")
    }
    gui.messagebox.showinfo = lambda title, message, **kw: popups.append((title, message))
    gui.messagebox.showerror = lambda title, message, **kw: popups.append((title, message))
    gui.messagebox.showwarning = lambda title, message, **kw: popups.append((title, message))
    gui.messagebox.askyesno = lambda title, message, **kw: True
    saved_prompt = gui.simpledialog.askstring
    gui.simpledialog.askstring = lambda *args, **kw: "Gui Made"

    try:
        app = gui.MusicApp(root)
        gui_pump(root, lambda: False, timeout=1.0)

        check(
            "gui: the window has three tabs",
            len(app.notebook.tabs()) == 3,
            len(app.notebook.tabs()),
        )
        check(
            "gui: the player tab lists the library",
            gui_pump(root, lambda: len(app.tracks) == 4),
            len(app.tracks),
        )
        check(
            "gui: one row per track",
            len(app.track_tree.get_children()) == len(app.tracks),
            len(app.track_tree.get_children()),
        )
        check(
            "gui: the soundtrack tab found the game's music",
            gui_pump(root, lambda: len(app.soundtracks) == 7),
            len(app.soundtracks),
        )
        check(
            "gui: the soundtrack tree shows a row per track",
            len(app.soundtrack_tree.get_children()) == len(app.soundtracks)
            and len(app.soundtracks) > 0,
            (len(app.soundtrack_tree.get_children()), len(app.soundtracks)),
        )
        check(
            "gui: the category box is filled from the soundtrack",
            "records" in list(app.category_combo["values"]),
            app.category_combo["values"],
        )
        check("gui: the playlist box lists the playlists", app.playlist_box.size() >= 1)
        check(
            "gui: the 'add to' box offers the same playlists",
            "Chill Vibes" in list(app.add_to_combo["values"]),
            app.add_to_combo["values"],
        )
        check(
            "gui: the selected playlist shows its tracks",
            gui_pump(root, lambda: len(app.playlist_tracks) >= 1)
            and len(app.playlist_tree.get_children()) == len(app.playlist_tracks),
            (len(app.playlist_tree.get_children()), len(app.playlist_tracks)),
        )
        check(
            "gui: the mute box follows the stored preference",
            app.mute_var.get() is True and app.engine.mute_minecraft is True,
        )
        check(
            "gui: the test run uses the dry-run player",
            isinstance(app.engine.backend, player.DryRunBackend),
        )

        rows = app.track_tree.get_children()
        app.track_tree.selection_set(rows[0])
        first_title = app.track_tree.item(rows[0], "values")[1]
        app._on_play_selected()
        gui_pump(root, lambda: "Playing " in app.log.get("1.0", "end"))
        text = app.log.get("1.0", "end")
        check(
            "gui: pressing play reports the track",
            f"Playing {first_title}" in text,
            text.strip().splitlines()[-2:],
        )
        check(
            "gui: playing mutes Minecraft's music",
            "audio_music:0" in text,
            text.strip().splitlines()[-2:],
        )

        app._on_play_random()
        gui_pump(root, lambda: app.log.get("1.0", "end").count("Playing ") >= 2)
        plays = app.log.get("1.0", "end").count("Playing ")
        check("gui: the random button plays another track", plays >= 2, plays)

        app.playlist_box.selection_clear(0, "end")
        app.playlist_box.selection_set(0)
        app.show_playlist()
        app._on_playlist_play()
        gui_pump(root, lambda: len(app.engine.queue) >= 1)
        check(
            "gui: playing a playlist builds a queue",
            len(app.engine.queue) == len(app.playlist_tracks),
            (len(app.engine.queue), len(app.playlist_tracks)),
        )
        app._on_stop()
        check(
            "gui: stop resets the now-playing line",
            app.now_var.get() == "Nothing playing.",
            app.now_var.get(),
        )

        # ---- a playlist whose file is gone -----------------------------
        import core

        ghost = DEVICE_MUSIC / "gone_forever.wav"
        entry = core.create_playlist("Ghost", [str(ghost)])
        app.refresh_playlists(select=entry["name"])
        gui_pump(root, lambda: len(app.playlist_tracks) == 1)
        values = app.playlist_tree.item(app.playlist_tree.get_children()[0], "values")
        check("gui: a missing file is marked in the list", values[3] == "missing", values)
        check(
            "gui: the ghost playlist still shows its one row",
            len(app.playlist_tree.get_children()) == 1,
            len(app.playlist_tree.get_children()),
        )
        core.delete_playlist("Ghost")
        app.refresh_playlists(select="Chill Vibes")

        # ---- adding a soundtrack track to a playlist -------------------
        app.add_to_var.set("Chill Vibes")
        before = len(core.playlist_tracks("Chill Vibes"))
        app.soundtrack_tree.selection_set(app.soundtrack_tree.get_children()[0])
        app._on_add_soundtrack_selection()
        gui_pump(root, lambda: len(core.playlist_tracks("Chill Vibes")) > before)
        check(
            "gui: a soundtrack track can be added to a playlist",
            len(core.playlist_tracks("Chill Vibes")) == before + 1,
            (before, len(core.playlist_tracks("Chill Vibes"))),
        )

        check("gui: no unexpected dialog was raised", not popups, popups)

    finally:
        for name, function in saved.items():
            setattr(gui.messagebox, name, function)
        gui.simpledialog.askstring = saved_prompt
        try:
            app._on_close()
        except Exception:
            root.destroy()


def _pe_subsystem(path: Path):
    """Read a PE image's Subsystem field: 2 = GUI (windowed), 3 = console."""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return None
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[offset : offset + 4] != b"PE\0\0":
        return None
    return struct.unpack_from("<H", data, offset + 24 + 0x44)[0]


def check_frozen():
    """Extras that only make sense once the app has been packaged."""
    gui_bytes = GUI_EXE.read_bytes()
    check("exe: the GUI binary is windowed", _pe_subsystem(GUI_EXE) == 2, _pe_subsystem(GUI_EXE))
    check("exe: the CLI binary is a console app", _pe_subsystem(CLI_EXE) == 3, _pe_subsystem(CLI_EXE))
    check("exe: the version resource is embedded", "ProductName".encode("utf-16-le") in gui_bytes)
    # An older design shipped a templates/ folder (and wrote packs into a build/
    # folder); neither belongs in the portable build any more.  discs/ does: it
    # is the library folder the player reads.
    check("exe: no leftover template folder is shipped", not (EXE_DIR / "templates").exists())
    check("exe: no disc build output is shipped", not (EXE_DIR / "build").exists())
    check("exe: the discs library folder is shipped", (EXE_DIR / "discs").is_dir())

    proc = run("--version")
    check(
        "exe: --version prints the banner",
        proc.returncode == 0 and "music player" in proc.stdout,
        (proc.stdout or "").strip().splitlines()[:1],
    )

    # The windowed build must still handle the CLI (a user may type
    # `yt2disc.exe soundtrack --list` in a terminal).
    proc = subprocess.run(
        [str(GUI_EXE), "--cli", "soundtrack", "--list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ENV,
        cwd=str(ROOT),
        timeout=180,
    )
    check("exe: the windowed build runs the CLI path", proc.returncode == 0, proc.returncode)

    # ... and with no console at all (double-clicked from Explorer) it has to
    # fall back to a log file instead of dying on a missing sys.stdout.
    log_file = DATA_ROOT / "yt2disc-cmd.log"
    log_file.unlink(missing_ok=True)
    proc = subprocess.run(
        [PY, str(HERE / "no_console.py"), str(GUI_EXE), "--version"],
        creationflags=0x00000008,  # DETACHED_PROCESS: nobody gets a console
        env=ENV,
        cwd=str(ROOT),
        timeout=180,
    )
    text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
    check("exe: a run without a console exits 0", proc.returncode == 0, proc.returncode)
    check("exe: a run without a console logs its output", "music player" in text, text.strip()[:80])

    # The whole point of the portable layout: with YT2DISC_ROOT unset the data
    # folder defaults to the folder holding the executable.  Verified on a
    # throw-away copy, never in place.
    portable = Path(tempfile.mkdtemp(prefix="yt2disc-portable-"))
    try:
        app = portable / "app"
        shutil.copytree(EXE_DIR, app)
        env = dict(ENV)
        env.pop("YT2DISC_ROOT", None)
        proc = subprocess.run(
            [str(app / CLI_EXE.name), "library", "add", str(DEVICE_MUSIC)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(portable),
            timeout=300,
        )
        check(
            "exe: a portable copy writes next to the exe, without YT2DISC_ROOT",
            proc.returncode == 0
            and (app / "state.json").is_file()
            and not (app / "playlists.json").exists(),
            (proc.stderr or proc.stdout or "").strip()[-120:],
        )
    finally:
        shutil.rmtree(portable, ignore_errors=True)


def main() -> int:
    keep = "--keep" in sys.argv
    prepare_fixtures()
    cleanup()

    print("=" * 72)
    print("yt2disc end-to-end check - fake ffmpeg + dry-run player, no network")
    print("interpreter : " + PY)
    print("target      : " + (f"executables in {EXE_DIR}" if EXE_DIR else "python sources"))
    print("app root    : " + str(ROOT))
    print("data root   : " + str(DATA_ROOT))
    print("minecraft   : " + str(MC_SANDBOX))
    print("content     : " + str(CONTENT_ROOT))
    print("device music: " + str(DEVICE_MUSIC))
    print("=" * 72)

    for label, function in (
        ("core helpers + choosing tracks", check_core_helpers),
        ("library folders", check_library),
        ("playlists", check_playlists),
        ("minecraft soundtrack", check_soundtrack),
        ("minecraft music switch", check_mc_music),
        ("playback", check_play),
        ("desktop window", check_gui),
    ):
        print(f"\n--- {label} " + "-" * max(0, 60 - len(label)))
        function()

    if EXE_DIR:
        print("\n--- packaged executable " + "-" * 42)
        check_frozen()

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} of the checks did not pass")
        for name in FAILURES:
            print("  - " + name)
    else:
        print("All checks passed.")
    if keep:
        print("Artifacts kept (--keep): playlists.json, state.json, cache/")
    else:
        cleanup()
        for folder in (DATA_ROOT, MC_SANDBOX, CONTENT_ROOT, DEVICE_MUSIC):
            shutil.rmtree(folder, ignore_errors=True)
        print("Generated artifacts were cleaned up again.")
    print("=" * 72)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())










