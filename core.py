"""core.py - shared engine for yt2disc.

yt2disc is a small, portable music player for the tracks a Minecraft player
already owns: the audio files on their own disk, and the soundtrack that ships
inside Minecraft for Windows.  It builds playlists, plays a named track or a
random one, and can mute Minecraft's own background music while it plays - so a
custom song is never fighting the game's soundtrack.

This module holds the data and Minecraft plumbing and contains *no* UI imports,
so ``gui.py`` and ``cli.py`` share every code path.  All paths are resolved
relative to this file's directory so the whole folder stays portable (drop it
on a USB stick and run it).

Everything the user owns lives in the *data* root, next to the executable:

    bin/ffmpeg          the one helper binary - ffmpeg.exe on Windows, and a
                        fallback elsewhere, because a system ffmpeg that a
                        package manager or the host's image put on PATH is
                        used too (see bin/README.txt)
    cache/              transcoded audio, safe to delete at any time
    playlists.json      the playlists
    state.json          settings: library folders, mute preference, history

``YT2DISC_ROOT`` overrides the data root (used by the end-to-end test, and
handy to keep your playlists somewhere writable while the exe sits elsewhere).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------
#
# Two different roots are involved:
#
# * the *data* root (``SCRIPT_DIR``) holds bin/, cache/, playlists.json and
#   state.json - everything the user owns, so it has to stay writable.  Run
#   from source it is this folder; inside a frozen executable (PyInstaller) it
#   is the folder that contains the .exe, because the code itself lives in a
#   throw-away extraction folder.
# * the *game* folders, which are only ever read - or written with an explicit
#   backup: the Minecraft soundtrack and options.txt.


def is_frozen() -> bool:
    """True when running from a PyInstaller style executable."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Writable folder that holds bin/, cache/ and the JSON files."""
    override = os.environ.get("YT2DISC_ROOT")
    if override:
        try:
            root = Path(override).expanduser()
            root.mkdir(parents=True, exist_ok=True)
            return root.resolve()
        except OSError:
            pass
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


SCRIPT_DIR = app_dir()
BIN_DIR = SCRIPT_DIR / "bin"
CACHE_DIR = SCRIPT_DIR / "cache"
# Where the hosted converter's downloads are meant to land.  When the folder
# exists it is always part of the library, so a file converted in a browser and
# dropped in here turns up in the player without being added by hand.
DISCS_DIR = SCRIPT_DIR / "discs"
PLAYLISTS_PATH = SCRIPT_DIR / "playlists.json"
STATE_PATH = SCRIPT_DIR / "state.json"

# What we are willing to load.  The list is deliberately generous - ffmpeg
# decodes all of these and turns them into the WAV that the player needs.
AUDIO_EXTENSIONS = (
    ".ogg",
    ".oga",
    ".opus",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".wav",
    ".wma",
    ".aif",
    ".aiff",
)

# The vanilla soundtrack ships as a resource pack of plain Ogg Vorbis files.
VANILLA_MUSIC_PACK = "vanilla_music"
VANILLA_MUSIC_TAIL = (
    "data",
    "resource_packs",
    VANILLA_MUSIC_PACK,
    "sounds",
    "music",
)
# 'records' are the music discs, 'menu' the title-screen tracks.
MUSIC_CATEGORIES = ("game", "creative", "end", "nether", "records", "water", "menu")

# Bedrock keeps its volume sliders in a plain text file.  'audio_music' is the
# background soundtrack, 'audio_record' the jukebox discs.
OPTIONS_TAIL = ("minecraftpe", "options.txt")
MUSIC_OPTION_KEY = "audio_music"
RECORD_OPTION_KEY = "audio_record"
# One backup of the untouched file, so 'mc-music --on' can always undo us.
OPTIONS_BACKUP_SUFFIX = ".yt2disc.bak"

# Environment overrides, all used by the end-to-end test.
MINECRAFT_ROOT_ENV = "YT2DISC_MINECRAFT"
MINECRAFT_CONTENT_ENV = "YT2DISC_MINECRAFT_CONTENT"
FAKE_PLAYER_ENV = "YT2DISC_FAKE_PLAYER"

FFMPEG_DOWNLOAD_URL = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
)
FFMPEG_DOWNLOAD_URL_ALT = "https://github.com/BtbN/FFmpeg-Builds/releases"

# The player asks the OS for plain uncompressed audio, so every track is
# decoded to 16-bit stereo PCM at the CD sample rate.
WAV_SAMPLE_RATE = 44100
WAV_CHANNELS = 2

_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")

# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class YT2DiscError(Exception):
    """Base class for every recoverable error raised by core."""


class BinaryMissingError(YT2DiscError):
    """The required external binary (ffmpeg) could not be located."""

    def __init__(self, tool: str, searched: list[str] | None = None):
        self.tool = tool
        self.searched = searched or []
        super().__init__(
            f"required binary '{tool}' was not found. "
            f"Place it in '{BIN_DIR}' (see README) or put it on your PATH."
        )


class InputError(YT2DiscError):
    """The user pointed at something we cannot use (missing file, bad index)."""


class PlaylistError(YT2DiscError):
    """A playlist is missing, or a name is already taken."""


class OptionsError(YT2DiscError):
    """Minecraft's options.txt could not be read or trusted."""


# --------------------------------------------------------------------------
# Tiny helpers
# --------------------------------------------------------------------------


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def fmt_duration(seconds) -> str:
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "--:--"
    if total < 0:
        total = 0
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def human_size(num) -> str:
    try:
        size = float(num)
    except (TypeError, ValueError):
        return "?"
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024.0
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} GiB"


def slugify(text: str) -> str:
    """ASCII, lower-case, underscore separated slug."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = "playlist"
    if text[0].isdigit():
        text = "x" + text
    return text[:48].strip("_") or "playlist"


def track_title(path) -> str:
    """A readable name for a file: ``sweden.ogg`` -> ``sweden``."""
    return Path(path).stem.replace("_", " ").strip() or Path(path).name


# --------------------------------------------------------------------------
# JSON persistence (playlists.json = playlists, state.json = settings)
# --------------------------------------------------------------------------


def _read_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        try:  # keep a copy of an unreadable file so nothing is silently lost
            shutil.copy2(path, str(path) + ".bak")
        except OSError:
            pass
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def ensure_dirs() -> None:
    """Create the writable data folders."""
    for directory in (BIN_DIR, CACHE_DIR, DISCS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


# ---- settings (state.json) -------------------------------------------------


def load_state() -> dict:
    data = _read_json(STATE_PATH, None)
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("library_roots"), list):
        data["library_roots"] = []
    if not isinstance(data.get("recent"), list):
        data["recent"] = []
    if not isinstance(data.get("mute_minecraft_music"), bool):
        data["mute_minecraft_music"] = True
    data.setdefault("last_played", None)
    data.setdefault("format_version", 1)
    return data


def save_state(data: dict) -> None:
    _write_json(STATE_PATH, data)


def get_setting(key: str, default=None):
    """One value out of state.json, with a sensible fallback."""
    value = load_state().get(key)
    return default if value is None else value


def set_setting(key: str, value) -> dict:
    state = load_state()
    state[key] = value
    save_state(state)
    return state


def mute_preference() -> bool:
    """True when the player should silence Minecraft's own music while it plays."""
    return bool(get_setting("mute_minecraft_music", True))


def remember_played(track, state=None) -> None:
    """Note the most recent track (and keep a short 'recent' history)."""
    state = load_state() if state is None else state
    path = str((track or {}).get("path") or track or "")
    if not path:
        return
    state["last_played"] = {"path": path, "at": now_iso()}
    recent = [entry for entry in state.get("recent", []) if entry != path]
    recent.insert(0, path)
    state["recent"] = recent[:25]
    save_state(state)


# ---- playlists (playlists.json) -------------------------------------------


def load_playlists() -> dict:
    data = _read_json(PLAYLISTS_PATH, None)
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("playlists"), list):
        data["playlists"] = []
    data.setdefault("format_version", 1)
    return data


def save_playlists(data: dict) -> None:
    _write_json(PLAYLISTS_PATH, data)


def _clean_tracks(tracks) -> list[str]:
    """Absolute paths only, duplicates removed, order kept."""
    cleaned: list[str] = []
    for item in tracks or ():
        if not item:
            continue
        path = str(Path(str(item)).expanduser())
        if path not in cleaned:
            cleaned.append(path)
    return cleaned


def list_playlists(playlists=None) -> list[dict]:
    """Every playlist, A-Z by name."""
    data = load_playlists() if playlists is None else playlists
    entries = [entry for entry in data.get("playlists", []) if isinstance(entry, dict)]
    return sorted(entries, key=lambda entry: str(entry.get("name", "")).lower())


def find_playlist(name, playlists=None) -> dict | None:
    """Look a playlist up by name or by its slug id."""
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    for entry in list_playlists(playlists):
        if wanted in (str(entry.get("name", "")).lower(), str(entry.get("id", "")).lower()):
            return entry
    return None


def require_playlist(name, playlists=None) -> dict:
    entry = find_playlist(name, playlists)
    if entry is None:
        known = ", ".join(f"'{item.get('name')}'" for item in list_playlists(playlists))
        raise PlaylistError(f"no playlist called '{name}' (found: {known or 'none'})")
    return entry


def _entry_in(data: dict, name):
    """The *mutable* playlist dict inside *data*, or ``None``."""
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    for entry in data.get("playlists", []):
        if not isinstance(entry, dict):
            continue
        if wanted in (
            str(entry.get("name", "")).lower(),
            str(entry.get("id", "")).lower(),
        ):
            return entry
    return None


def create_playlist(name, tracks=(), playlists=None) -> dict:
    """Make a new (possibly empty) playlist and return it."""
    data = load_playlists() if playlists is None else playlists
    label = str(name or "").strip()
    if not label:
        raise PlaylistError("a playlist needs a name")
    if _entry_in(data, label) is not None:
        raise PlaylistError(f"a playlist called '{label}' already exists")

    taken = {
        str(entry.get("id"))
        for entry in data.get("playlists", [])
        if isinstance(entry, dict)
    }
    identifier = slugify(label)
    unique = identifier
    suffix = 2
    while unique in taken:
        unique = f"{identifier}_{suffix}"
        suffix += 1

    stamp = now_iso()
    entry = {
        "id": unique,
        "name": label,
        "tracks": _clean_tracks(tracks),
        "created": stamp,
        "updated": stamp,
    }
    data.setdefault("playlists", []).append(entry)
    save_playlists(data)
    return entry


def rename_playlist(name, new_name, playlists=None) -> dict:
    data = load_playlists() if playlists is None else playlists
    entry = _entry_in(data, name) or require_playlist(name, data)
    label = str(new_name or "").strip()
    if not label:
        raise PlaylistError("a playlist needs a name")
    clash = _entry_in(data, label)
    if clash is not None and clash is not entry:
        raise PlaylistError(f"a playlist called '{label}' already exists")
    entry["name"] = label
    entry["id"] = slugify(label)
    entry["updated"] = now_iso()
    save_playlists(data)
    return entry


def delete_playlist(name, playlists=None) -> bool:
    data = load_playlists() if playlists is None else playlists
    entry = _entry_in(data, name)
    if entry is None:
        return False
    data["playlists"] = [
        item for item in data.get("playlists", []) if item is not entry
    ]
    save_playlists(data)
    return True


def add_to_playlist(name, paths, playlists=None) -> dict:
    """Append tracks to a playlist; returns what was added and what was already there."""
    data = load_playlists() if playlists is None else playlists
    entry = _entry_in(data, name) or require_playlist(name, data)
    current = list(entry.get("tracks") or [])
    added: list[str] = []
    skipped: list[str] = []
    for path in _clean_tracks(paths):
        if path in current:
            skipped.append(path)
            continue
        current.append(path)
        added.append(path)
    if added:
        entry["tracks"] = current
        entry["updated"] = now_iso()
        save_playlists(data)
    return {"playlist": entry, "added": added, "skipped": skipped, "total": len(current)}


def remove_from_playlist(name, selector, playlists=None) -> dict:
    """Drop one track: ``selector`` is a 1-based index, a path or a file name."""
    data = load_playlists() if playlists is None else playlists
    entry = _entry_in(data, name) or require_playlist(name, data)
    tracks = list(entry.get("tracks") or [])
    if not tracks:
        raise InputError(f"'{entry.get('name')}' has no tracks to remove")

    removed = None
    text = str(selector or "").strip()
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(tracks):
            removed = tracks.pop(index - 1)
    if removed is None:
        wanted = text.lower()
        for candidate in list(tracks):
            if wanted in (candidate.lower(), Path(candidate).name.lower()):
                tracks.remove(candidate)
                removed = candidate
                break
    if removed is None:
        raise InputError(
            f"'{selector}' is not in '{entry.get('name')}' "
            f"({len(tracks)} track(s); use the index or the full path)"
        )

    entry["tracks"] = tracks
    entry["updated"] = now_iso()
    save_playlists(data)
    return {"playlist": entry, "removed": removed, "total": len(tracks)}


def playlist_tracks(name, playlists=None, existing_only: bool = False) -> list[dict]:
    """The tracks of one playlist, as full track dicts (missing files flagged)."""
    entry = require_playlist(name, playlists)
    tracks = [track_from_path(path) for path in entry.get("tracks") or []]
    if existing_only:
        tracks = [track for track in tracks if track["exists"]]
    return tracks


# --------------------------------------------------------------------------
# The library: audio files on this device
# --------------------------------------------------------------------------
#
# A *track* is a plain dict so it can travel through the CLI, the GUI and the
# player without any of them depending on each other.
VANILLA_MARKER = os.sep + VANILLA_MUSIC_PACK.lower() + os.sep


def is_audio_file(path) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXTENSIONS


def is_vanilla_track(path) -> bool:
    """True for a file that lives inside Minecraft's own soundtrack pack."""
    return VANILLA_MARKER in str(path).lower().replace("/", os.sep)


def vanilla_category(path) -> str:
    """'game', 'records', 'menu', ... - which part of the soundtrack a file is."""
    parts = [part.lower() for part in Path(path).parts]
    if "music" not in parts:
        return "game"
    index = len(parts) - 1 - parts[::-1].index("music")
    for part in parts[index + 1 :]:
        if part in MUSIC_CATEGORIES and part != "game":
            return part
    return "game"


def track_from_path(path, duration=None) -> dict:
    """Describe one audio file. Missing files are reported, never hidden."""
    file_path = Path(str(path))
    size = 0
    exists = file_path.is_file()
    if exists:
        try:
            size = file_path.stat().st_size
        except OSError:  # pragma: no cover - defensive
            size = 0
    vanilla = is_vanilla_track(file_path)
    return {
        "path": str(file_path),
        "file": file_path.name,
        "name": track_title(file_path),
        "folder": str(file_path.parent),
        "extension": file_path.suffix.lower(),
        "size": size,
        "size_text": human_size(size),
        "duration": duration,
        "duration_text": fmt_duration(duration) if duration else "--:--",
        "vanilla": vanilla,
        "category": vanilla_category(file_path) if vanilla else "",
        "exists": exists,
    }


def describe_track(track) -> str:
    """One readable line, e.g. ``sweden  (04:07, 3.4 MiB)``."""
    if not isinstance(track, dict):
        track = track_from_path(track)
    bits = [track.get("name") or track.get("file") or "?"]
    if track.get("duration"):
        bits.append(track["duration_text"])
    if track.get("size"):
        bits.append(track.get("size_text") or human_size(track["size"]))
    if not track.get("exists", True):
        bits.append("MISSING")
    return f"{bits[0]}  ({', '.join(bits[1:])})" if len(bits) > 1 else bits[0]


def scan_audio_folder(root, recursive: bool = True, log=None) -> list[dict]:
    """Every audio file in a folder, A-Z.  Raises when the folder is not there."""
    start = Path(str(root)).expanduser()
    if not start.is_dir():
        raise InputError(f"'{root}' is not a folder")
    found: list[Path] = []
    walker = start.rglob("*") if recursive else start.glob("*")
    for entry in walker:
        try:
            if entry.is_file() and is_audio_file(entry):
                found.append(entry)
        except OSError:  # pragma: no cover - defensive
            continue
    found.sort(key=lambda item: str(item).lower())
    if log:
        log(f"{len(found)} audio file(s) in {start}")
    return [track_from_path(path) for path in found]


# ---- remembered library folders (state.json) ------------------------------


def library_roots(state=None) -> list[str]:
    state = load_state() if state is None else state
    return [str(Path(root).expanduser()) for root in state.get("library_roots") or []]


def add_library_root(folder, state=None) -> dict:
    """Remember a folder so the library can be rebuilt with one call."""
    start = Path(str(folder)).expanduser()
    if not start.is_dir():
        raise InputError(f"'{folder}' is not a folder")
    state = load_state() if state is None else state
    roots = library_roots(state)
    resolved = str(start.resolve())
    added = resolved not in roots
    if added:
        roots.append(resolved)
        state["library_roots"] = roots
        save_state(state)
    return {"folder": resolved, "added": added, "roots": roots}


def remove_library_root(folder, state=None) -> dict:
    state = load_state() if state is None else state
    resolved = str(Path(str(folder)).expanduser().resolve())
    roots = library_roots(state)
    remaining = [root for root in roots if root != resolved]
    state["library_roots"] = remaining
    save_state(state)
    return {
        "folder": resolved,
        "removed": len(remaining) != len(roots),
        "roots": remaining,
    }


def library_search_roots(state=None) -> list[str]:
    """The remembered folders, plus the discs folder when it is there.

    ``discs/`` is where converted downloads belong, so it joins the library on
    its own rather than having to be added like any other folder.
    """
    roots = library_roots(state)
    if DISCS_DIR.is_dir() and str(DISCS_DIR) not in roots:
        roots = roots + [str(DISCS_DIR)]
    return roots


def scan_library(state=None, recursive: bool = True, log=None) -> list[dict]:
    """Every track of every remembered folder, duplicates removed."""
    tracks: list[dict] = []
    seen: set[str] = set()
    for root in library_search_roots(state):
        try:
            found = scan_audio_folder(root, recursive=recursive)
        except InputError:
            if log:  # a folder that moved is skipped, not fatal
                log(f"skipped (gone): {root}")
            continue
        for track in found:
            key = track["path"].lower()
            if key not in seen:
                seen.add(key)
                tracks.append(track)
    tracks.sort(key=lambda track: (track["name"].lower(), track["path"].lower()))
    return tracks


def resolve_tracks(paths) -> list[dict]:
    """Turn user supplied paths into tracks, refusing anything unreadable."""
    tracks: list[dict] = []
    for item in paths or ():
        path = Path(str(item)).expanduser()
        if path.is_dir():
            tracks.extend(scan_audio_folder(path))
            continue
        if not path.is_file():
            raise InputError(f"'{item}' does not exist")
        if not is_audio_file(path):
            raise InputError(
                f"'{path.name}' is not a supported audio file "
                f"({', '.join(AUDIO_EXTENSIONS)})"
            )
        tracks.append(track_from_path(path))
    return tracks


# --------------------------------------------------------------------------
# Choosing what to play next
# --------------------------------------------------------------------------


def pick_random_track(tracks, avoid=None, rng=None) -> dict:
    """A random track, never the one that is already playing.

    ``rng`` is any object with ``choice`` (``random.Random()`` in the tests),
    which is what keeps this reproducible.
    """
    pool = [track for track in tracks or [] if isinstance(track, dict)]
    if not pool:
        raise InputError("there is nothing to play - add some tracks first")
    rng = rng or random
    current = str(avoid or "")
    if current and len(pool) > 1:
        pool = [track for track in pool if track.get("path") != current]
    return rng.choice(pool)


def next_track(tracks, current=None, wrap: bool = True) -> dict | None:
    """The track after ``current`` in list order (``None`` at the end)."""
    pool = [track for track in tracks or [] if isinstance(track, dict)]
    if not pool:
        return None
    if not current:
        return pool[0]
    paths = [track.get("path") for track in pool]
    if current in paths:
        index = paths.index(current) + 1
        if index < len(pool):
            return pool[index]
        return pool[0] if wrap else None
    return pool[0]


# --------------------------------------------------------------------------
# Locating the bundled binary / console plumbing
# --------------------------------------------------------------------------


def _candidate_names(tool: str) -> list[str]:
    return [f"{tool}.exe", tool] if os.name == "nt" else [tool]


def _executable(path: Path) -> Path | None:
    """Return ``path`` when it can really be started, else None.

    ``is_file()`` is not enough off Windows.  An ffmpeg that was extracted on
    another machine, copied in by a deploy step or unpacked from a tarball
    keeps whatever mode it arrived with, and a missing execute bit does not
    fail until ffmpeg is actually spawned - which the user only sees as a
    conversion dying for no visible reason.  So add the bit ourselves when we
    may, and treat a file that is still not runnable as absent, which lets the
    next candidate have a turn instead of shadowing it.
    """
    try:
        if not path.is_file():
            return None
        if os.name == "nt":  # Windows has no execute bit to check
            return path
        if not os.access(path, os.X_OK):
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path if os.access(path, os.X_OK) else None
    except OSError:
        # a read-only checkout, a vanished file, an odd filesystem: try elsewhere
        return None


def _env_override(tool: str) -> Path | None:
    key = "YT2DISC_" + tool.upper().replace("-", "_")
    value = os.environ.get(key)
    if value:
        return _executable(Path(value).expanduser())
    return None


def _bundled_binary(tool: str) -> Path | None:
    """A usable copy from bin/, if the user put one there."""
    for name in _candidate_names(tool):
        candidate = _executable(BIN_DIR / name)
        if candidate:
            return candidate
    return None


def find_binary(tool: str = "ffmpeg") -> Path | None:
    """Locate a helper binary, preferring the right copy for the platform.

    The order is deliberately different per platform:

    * ``YT2DISC_FFMPEG`` / ``YT2DISC_YT_DLP`` always wins, because it is an
      explicit instruction and the end-to-end test relies on it.
    * Windows then looks in ``bin/`` first: the portable build ships that
      folder next to the executable and has no package manager to fall back on.
    * Linux/macOS then try ``PATH`` first.  That is where ffmpeg normally
      comes from on a server - ``apt install ffmpeg``, a Dockerfile, or the
      host's own image, all of them patched and maintained - so a stale or
      unreadable leftover in ``bin/`` can no longer shadow it.

    Only after those does the other source get a turn, and a file that is not
    actually runnable is skipped at every step (see :func:`_executable`).
    """
    override = _env_override(tool)
    if override:
        return override

    if os.name != "nt":
        found = shutil.which(tool)
        if found:
            return Path(found)

    bundled = _bundled_binary(tool)
    if bundled:
        return bundled

    found = shutil.which(tool)
    return Path(found) if found else None


def missing_binaries() -> list[str]:
    return [tool for tool in ("ffmpeg",) if find_binary(tool) is None]


def binary_help_text() -> str:
    """What to tell someone whose machine has no ffmpeg, per platform."""
    if os.name == "nt":
        return (
            "yt2disc needs one helper program in the 'bin' folder:\n\n"
            f"  bin/ffmpeg.exe  <-  {FFMPEG_DOWNLOAD_URL}\n"
            f"                      (alternative: {FFMPEG_DOWNLOAD_URL_ALT})\n\n"
            "Unzip ffmpeg and copy ffmpeg.exe from its bin/ folder into bin/.\n\n"
            "ffmpeg is only used to decode a track into the WAV the player needs;\n"
            "the environment variable YT2DISC_FFMPEG can point at a custom copy,\n"
            "and any ffmpeg already on PATH is used as a last resort."
        )
    return (
        "yt2disc needs ffmpeg, which Linux and macOS normally have already:\n\n"
        "  Debian/Ubuntu   sudo apt install ffmpeg\n"
        "  Fedora          sudo dnf install ffmpeg\n"
        "  macOS           brew install ffmpeg\n\n"
        "ffmpeg on PATH is used first.  A copy in bin/ffmpeg works just as well -\n"
        "yt2disc adds a missing execute bit itself (chmod +x bin/ffmpeg does the\n"
        "same thing by hand).\n\n"
        "ffmpeg is only used to decode a track into the WAV the player needs, and\n"
        "the environment variable YT2DISC_FFMPEG can point at a custom copy."
    )


def ensure_console() -> bool:
    """Guarantee that ``print``/stderr have somewhere to go.

    A console-less (``--noconsole``) executable has ``sys.stdout is None``.  On
    Windows we first try to attach to the console of the process that started us
    (cmd.exe, PowerShell, a shortcut); when there is none we quietly write
    everything to ``yt2disc-cmd.log`` next to the app so no message is ever
    lost.  Returns True when real console output is available.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return True

    if os.name == "nt":
        try:
            import ctypes

            if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
                for name in ("stdout", "stderr"):
                    setattr(
                        sys,
                        name,
                        open(
                            "CONOUT$",
                            "w",
                            encoding="utf-8",
                            errors="replace",
                            buffering=1,
                        ),
                    )
                try:
                    sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
                except OSError:  # pragma: no cover - no console input available
                    sys.stdin = None
                return True
        except Exception:  # pragma: no cover - no console / not Windows
            pass

    try:
        stream = open(
            SCRIPT_DIR / "yt2disc-cmd.log",
            "w",
            encoding="utf-8",
            errors="replace",
            buffering=1,  # line buffered: the log stays readable while we run
        )
    except OSError:  # pragma: no cover - read-only folder
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return False


# --------------------------------------------------------------------------
# Decoding audio (ffmpeg)
# --------------------------------------------------------------------------
#
# The player feeds the OS a WAV file, so every track is decoded once into
# cache/ and then replayed from there.  The cache key is the file's identity
# (path + size + mtime), so editing a track in place produces a new file.


def _quiet_flags() -> dict:
    """Stop console windows from flashing when the GUI spawns helpers."""
    if os.name == "nt":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def _decode(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _parse_percent(line: str):
    match = _PERCENT_RE.search(line or "")
    if not match:
        return None
    try:
        return max(0.0, min(100.0, float(match.group(1))))
    except ValueError:
        return None


def _ffmpeg() -> Path:
    path = find_binary("ffmpeg")
    if path is None:
        raise BinaryMissingError("ffmpeg")
    return path


def _stream_subprocess(cmd, log=None, progress=None, tail_size: int = 40):
    """Run a command, streaming stdout lines into log()/progress().

    Returns ``(returncode, tail_lines)`` so callers can report the cause of a
    failure without dumping the whole decoder log.
    """
    if log:
        log("$ " + " ".join(str(part) for part in cmd))
    tail: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_quiet_flags(),
        )
    except OSError as exc:
        raise YT2DiscError(f"failed to start {cmd[0]}: {exc}") from exc

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        tail.append(line)
        if len(tail) > tail_size:
            del tail[0]
        percent = _parse_percent(line)
        if percent is not None and progress:
            progress(percent)
        if log:
            log(line)
    return proc.wait(), tail


def run_ffmpeg(cmd, log=None, progress=None, tail_size: int = 40):
    """Run an ffmpeg command, streaming its output lines (public wrapper).

    Returns ``(returncode, tail_lines)``.  The hosted converter needs exactly
    the behaviour the player relies on - run it, watch it, and keep the last
    few lines so a failure can quote ffmpeg instead of guessing - so this is
    exposed by name rather than by reaching for the underscore-prefixed helper.
    """
    return _stream_subprocess(cmd, log=log, progress=progress, tail_size=tail_size)


def probe_duration(path, ffmpeg=None) -> float | None:
    """Read a media duration straight out of ffmpeg's banner (no ffprobe)."""
    ffmpeg = ffmpeg or find_binary("ffmpeg")
    if ffmpeg is None:
        return None
    try:
        proc = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-i", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            **_quiet_flags(),
        )
    except (subprocess.SubprocessError, OSError):
        return None
    match = _DURATION_RE.search(proc.stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def describe_media(path) -> dict:
    """Duration + stream summary for one file (best effort)."""
    ffmpeg = find_binary("ffmpeg")
    if ffmpeg is None:
        return {"path": str(path), "duration": None, "streams": []}
    try:
        proc = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-i", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            **_quiet_flags(),
        )
    except (subprocess.SubprocessError, OSError):  # pragma: no cover - defensive
        return {"path": str(path), "duration": None, "streams": []}
    text = proc.stderr or ""
    streams = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("Stream #")
    ]
    return {"path": str(path), "duration": probe_duration(path, ffmpeg), "streams": streams}


def cache_path_for(source) -> Path:
    """Where the decoded WAV for *source* lives (stable across runs)."""
    path = Path(str(source))
    try:
        stat = path.stat()
        stamp = f"{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:  # pragma: no cover - defensive
        stamp = "0:0"
    key = hashlib.sha1(f"{path}|{stamp}".encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{slugify(path.stem)[:40]}-{key}.wav"


def prepare_playable(source, log=None, progress=None, force: bool = False) -> Path:
    """Decode *source* into the WAV the player hands to the OS (cached).

    Repeated plays of the same file reuse ``cache/<name>-<hash>.wav``; the hash
    covers size and modification time, so re-encoding a track invalidates it.
    """
    source = Path(str(source))
    if not source.is_file():
        raise InputError(f"'{source}' does not exist")
    ensure_dirs()
    destination = cache_path_for(source)
    if not force and destination.is_file() and destination.stat().st_size > 44:
        if log:
            log(f"cached  : {destination.name}")
        return destination

    ffmpeg = _ffmpeg()
    # The part file keeps the .wav suffix so ffmpeg can pick the right muxer
    # from the extension alone.
    tmp = destination.with_name(destination.stem + ".part" + destination.suffix)
    code, tail = _stream_subprocess(
        [
            str(ffmpeg),
            "-y",
            "-hide_banner",
            "-i",
            str(source),
            "-vn",
            "-ac",
            str(WAV_CHANNELS),
            "-ar",
            str(WAV_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(tmp),
        ],
        log=log,
        progress=progress,
    )
    if code != 0 or not tmp.is_file() or tmp.stat().st_size <= 44:
        try:
            tmp.unlink()
        except OSError:  # pragma: no cover - defensive
            pass
        detail = "\n".join(tail[-8:])
        raise YT2DiscError(f"ffmpeg could not decode '{source.name}':\n{detail}")
    os.replace(tmp, destination)
    if log:
        log(f"decoded : {source.name} -> {destination.name}")
    return destination


def cache_files() -> list[Path]:
    """Every decoded file currently in cache/."""
    if not CACHE_DIR.is_dir():
        return []
    return sorted(
        (path for path in CACHE_DIR.glob("*.wav") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def clear_cache(log=None) -> int:
    """Delete every decoded file; returns how many were removed."""
    removed = 0
    for path in list(CACHE_DIR.glob("*")) if CACHE_DIR.is_dir() else []:
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:  # pragma: no cover - defensive
            continue
    if log:
        log(f"cache cleared ({removed} file(s))")
    return removed


# --------------------------------------------------------------------------
# Finding the game folders
# --------------------------------------------------------------------------
#
# Two different places matter:
#
#   1. the *content* folder that ships the game files, which is where the
#      vanilla soundtrack lives
#   2. the per-account *data* folders inside %APPDATA%, which hold options.txt
#
# Windows moved both when the Store build became a GDK title, so every layout
# is listed; whatever is installed wins over the historical ones.

BEDROCK_APP_NAMES = ("Minecraft Bedrock", "Minecraft Bedrock Preview")
UWP_PACKAGE_NAMES = (
    "Microsoft.MinecraftUWP_8wekyb3d8bbwe",
    "Microsoft.MinecraftWindowsBeta_8wekyb3d8bbwe",
    "Microsoft.MinecraftPreview_8wekyb3d8bbwe",
)
# <drive>:\XboxGames\<title>\Content  (the GDK install)
GDK_TITLES = ("Minecraft for Windows", "Minecraft Preview for Windows")
# <Program Files>\WindowsApps\<package>\data  (the older UWP install)
DRIVE_LETTERS = "CDEFGHIJKLMNOPQRSTUVWXYZ"


def _mtime(path: Path) -> float:
    """Modification time of *path*, or ``0.0`` when it cannot be read."""
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - defensive
        return 0.0


def _is_legacy_layout(root: Path) -> bool:
    """True for a ``<Minecraft app>\\games\\com.mojang`` (pre-GDK) folder."""
    return (
        root.name == "com.mojang"
        and root.parent.name == "games"
        and root.parent.parent.name.startswith("Minecraft")
    )


def _looks_alive(root: Path) -> bool:
    """True when the game really keeps data of its own in *root*.

    A pre-GDK folder outlives an upgrade as an empty stub, and the game stopped
    reading it - so it only counts while it still holds worlds or settings.
    """
    if (root / "minecraftWorlds").is_dir() or (root / "minecraftpe").is_dir():
        return True
    return not _is_legacy_layout(root)


def _gdk_roots(base: Path) -> list[Path]:
    """The ``com.mojang`` folders of a GDK install, most likely one first."""
    roots = [base / "Users" / "Shared" / "games" / "com.mojang"]
    users = base / "Users"
    profiles: list[Path] = []
    if users.is_dir():
        try:
            entries = sorted(users.iterdir())
        except OSError:  # pragma: no cover - defensive
            entries = []
        for entry in entries:
            if entry.is_dir() and entry.name.lower() != "shared":
                profiles.append(entry / "games" / "com.mojang")
    # The account played most recently is the one the settings belong to.
    profiles.sort(key=_mtime, reverse=True)
    return roots + profiles


def bedrock_candidates() -> list[Path]:
    """Plausible Minecraft ``com.mojang`` data folders, most likely first."""
    override = os.environ.get(MINECRAFT_ROOT_ENV)
    if override:
        return [Path(override).expanduser()]

    candidates: list[Path] = []
    roaming = os.environ.get("APPDATA")
    if roaming:  # "Minecraft for Windows" keeps its data here
        for name in BEDROCK_APP_NAMES:
            base = Path(roaming) / name
            candidates.extend(_gdk_roots(base))
            candidates.append(base / "games" / "com.mojang")  # pre-GDK
    local = os.environ.get("LOCALAPPDATA")
    if local:  # the older UWP layout
        for package in UWP_PACKAGE_NAMES:
            candidates.append(
                Path(local)
                / "Packages"
                / package
                / "LocalState"
                / "games"
                / "com.mojang"
            )

    unique: list[Path] = []
    for candidate in candidates:  # one entry per folder, order kept
        if candidate not in unique:
            unique.append(candidate)
    return unique


def find_bedrock_dir(create: bool = False) -> Path | None:
    """Return the (existing) Bedrock ``com.mojang`` folder, else ``None``.

    Live folders win over pre-GDK stubs, so a stale ``games\\com.mojang`` left
    behind by an upgrade cannot shadow the folder the game really reads.
    """
    candidates = bedrock_candidates()
    for candidate in candidates:
        if candidate.is_dir() and _looks_alive(candidate):
            return candidate
    for candidate in candidates:  # only old stubs left
        if candidate.is_dir():
            return candidate
    if not create or not candidates:
        return None
    target = candidates[0]
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - defensive
        return None
    return target


def minecraft_running() -> bool:
    """True while a Bedrock build looks alive (best effort, Windows only)."""
    if os.name != "nt":
        return False
    try:
        proc = subprocess.run(
            ["tasklist", "/nh"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            **_quiet_flags(),
        )
    except Exception:  # pragma: no cover - tasklist unavailable
        return False
    return "minecraft.windows.exe" in _decode(proc.stdout).lower()


def minecraft_content_roots() -> list[Path]:
    """Plausible Minecraft *content* folders (the ones shipping the game files)."""
    override = os.environ.get(MINECRAFT_CONTENT_ENV)
    if override:
        return [Path(override).expanduser()]

    candidates: list[Path] = []
    system_drive = os.environ.get("SystemDrive") or "C:"
    for letter in dict.fromkeys([system_drive[:1], *DRIVE_LETTERS]):
        for title in GDK_TITLES:
            candidates.append(Path(f"{letter}:\\XboxGames") / title / "Content")
    program_files = os.environ.get("ProgramFiles")
    if program_files:  # the older UWP layout keeps everything under the package
        windows_apps = Path(program_files) / "WindowsApps"
        for package in UWP_PACKAGE_NAMES:
            try:
                for entry in sorted(windows_apps.glob(f"{package}_*")):
                    candidates.append(entry / "data")
            except OSError:  # pragma: no cover - defensive
                continue

    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def vanilla_music_root(content=None) -> Path | None:
    """The folder holding Minecraft's own soundtrack, or ``None``.

    Pointing straight at a ``...\\sounds\\music`` folder works too, which is what
    the "pick a folder" button in the GUI may hand us.
    """
    if content is not None:
        direct = Path(str(content)).expanduser()
        if direct.is_dir() and direct.name.lower() == "music":
            return direct
        root = direct.joinpath(*VANILLA_MUSIC_TAIL)
        return root if root.is_dir() else None
    for candidate in minecraft_content_roots():
        root = candidate.joinpath(*VANILLA_MUSIC_TAIL)
        if root.is_dir():
            return root
    return None


def find_minecraft_content() -> Path | None:
    """The content folder whose soundtrack we can actually read."""
    for candidate in minecraft_content_roots():
        if candidate.joinpath(*VANILLA_MUSIC_TAIL).is_dir():
            return candidate
    return None


def list_vanilla_music(content=None, category=None, search=None, log=None) -> list[dict]:
    """Minecraft's own soundtrack, optionally filtered by category or text.

    Returns an empty list when the game files cannot be found - the caller
    decides how loud to be about that.
    """
    root = vanilla_music_root(content)
    if root is None:
        return []
    tracks = scan_audio_folder(root)
    wanted = str(category or "").strip().lower()
    if wanted and wanted not in ("all", "any"):
        tracks = [track for track in tracks if track["category"] == wanted]
    needle = str(search or "").strip().lower()
    if needle:
        tracks = [
            track
            for track in tracks
            if needle in track["name"].lower() or needle in track["file"].lower()
        ]
    if log:
        log(f"{len(tracks)} vanilla track(s) in {root}")
    return tracks


def soundtrack_summary(content=None) -> list[dict]:
    """``[{'category', 'count'}]`` for the categories that actually hold tracks."""
    tracks = list_vanilla_music(content)
    counts: dict[str, int] = {}
    for track in tracks:
        counts[track["category"]] = counts.get(track["category"], 0) + 1
    order = [name for name in MUSIC_CATEGORIES if name in counts]
    order += sorted(name for name in counts if name not in MUSIC_CATEGORIES)
    return [{"category": name, "count": counts[name]} for name in order]


def resolve_vanilla_track(selector, content=None) -> dict:
    """Find one soundtrack track by 1-based index, file name or title."""
    tracks = list_vanilla_music(content)
    if not tracks:
        raise InputError(
            "Minecraft's soundtrack was not found - is Minecraft for Windows "
            "installed? Set YT2DISC_MINECRAFT_CONTENT to its Content folder."
        )
    text = str(selector or "").strip()
    if not text:
        raise InputError("give a track number, file name or title")
    if text.isdigit() and 1 <= int(text) <= len(tracks):
        return tracks[int(text) - 1]
    wanted = text.lower()
    for track in tracks:
        if wanted in (track["name"].lower(), track["file"].lower(), track["path"].lower()):
            return track
    for track in tracks:  # a partial title is good enough
        if wanted in track["name"].lower():
            return track
    raise InputError(f"no soundtrack track matches '{selector}'")


# --------------------------------------------------------------------------
# Minecraft's own music switch (minecraftpe/options.txt)
# --------------------------------------------------------------------------
#
# The game stores every volume slider in a plain ``key:value`` text file.  The
# background soundtrack is ``audio_music``, the jukebox discs ``audio_record``;
# writing 0 silences that channel and nothing else.  We keep one backup of the
# untouched file so the change can always be undone, and we never guess: an
# options.txt that cannot be parsed raises instead of being overwritten.


def options_candidates(mc_dir=None) -> list[Path]:
    """Every ``minecraftpe/options.txt`` the installed build might use."""
    if mc_dir is not None:
        return [Path(str(mc_dir)).expanduser().joinpath(*OPTIONS_TAIL)]
    return [root.joinpath(*OPTIONS_TAIL) for root in bedrock_candidates()]


def find_options_file(mc_dir=None, create: bool = False) -> Path | None:
    """The options.txt the game really uses, else ``None``.

    With ``create`` the most plausible location is returned even when nothing is
    there yet - the parent folder is created first so we never invent a whole
    new tree the game would ignore.
    """
    candidates = options_candidates(mc_dir)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if not create or not candidates:
        return None
    for candidate in candidates:  # a folder the game already made
        if candidate.parent.is_dir():
            return candidate
    target = candidates[0]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - defensive
        return None
    return target


def parse_options(text: str):
    """``(values, order)``: the key->value map plus the order keys appeared in."""
    values: dict[str, str] = {}
    order: list[str] = []
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        if key not in values:
            order.append(key)
        values[key] = value
    return values, order


def render_options(values: dict, order=None) -> str:
    """Rebuild the file: known order first, then anything new, nothing lost."""
    keys = list(order or [])
    keys += [key for key in values if key not in keys]
    lines = [f"{key}:{values[key]}" for key in keys if key in values]
    return "\n".join(lines) + "\n"


def _as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_option_value(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def read_minecraft_options(mc_dir=None) -> dict:
    """The current volume sliders, or a report saying there is no file yet."""
    path = find_options_file(mc_dir)
    report = {
        "path": None,
        "values": {},
        "music": None,
        "record": None,
        "music_muted": None,
        "exists": False,
        "minecraft_running": False,
    }
    if path is None:
        return report
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - defensive
        raise OptionsError(f"could not read '{path}' ({exc})") from exc
    values, _order = parse_options(text)
    music = _as_number(values.get(MUSIC_OPTION_KEY))
    record = _as_number(values.get(RECORD_OPTION_KEY))
    report.update(
        {
            "path": path,
            "values": values,
            "music": music,
            "record": record,
            "music_muted": music == 0.0,
            "exists": True,
        }
    )
    return report


def set_minecraft_options(updates: dict, mc_dir=None, log=None) -> dict:
    """Write volume sliders, keeping one backup of the previous file.

    ``updates`` maps option keys (``audio_music`` / ``audio_record``) to the
    value they should get.  Returns ``{'path', 'backup', 'before', 'after',
    'changed', 'minecraft_running'}``.
    """
    if not updates:
        raise InputError("nothing to change")
    path = find_options_file(mc_dir, create=True)
    if path is None:
        raise OptionsError(
            "Minecraft's settings folder was not found - run the game once so "
            "it creates one, or set YT2DISC_MINECRAFT to your com.mojang folder."
        )

    text = ""
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise OptionsError(f"could not read '{path}' ({exc})") from exc
        values, order = parse_options(text)
        if not values and text.strip():
            raise OptionsError(
                f"'{path}' does not look like Minecraft's options file - "
                "leaving it alone."
            )
    else:
        values, order = {}, []

    before = dict(values)
    for key, value in updates.items():
        values[key] = _format_option_value(value)
    after = dict(values)
    changed = before != after

    backup = None
    if path.is_file() and changed:
        candidate = path.with_name(path.name + OPTIONS_BACKUP_SUFFIX)
        if not candidate.exists():
            try:
                shutil.copy2(path, candidate)
                backup = candidate
                if log:
                    log(f"Kept a copy of the previous settings: {candidate}")
            except OSError:  # pragma: no cover - defensive
                backup = None

    if changed:
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                render_options(values, order), encoding="utf-8", newline="\n"
            )
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover - defensive
                pass
            raise OptionsError(
                f"could not write '{path}' ({exc}) - is Minecraft still running?"
            ) from exc

    return {
        "path": path,
        "backup": backup,
        "before": before,
        "after": after,
        "changed": changed,
        "minecraft_running": minecraft_running(),
    }


def mute_minecraft_music(mc_dir=None, log=None, also_records: bool = False) -> dict:
    """Silence the game's background music (and optionally the jukebox too)."""
    updates = {MUSIC_OPTION_KEY: 0.0}
    if also_records:
        updates[RECORD_OPTION_KEY] = 0.0
    return set_minecraft_options(updates, mc_dir=mc_dir, log=log)


def unmute_minecraft_music(mc_dir=None, log=None) -> dict:
    """Turn the game's music back on."""
    if find_options_file(mc_dir) is None:
        return {
            "path": None,
            "backup": None,
            "before": {},
            "after": {},
            "changed": False,
            "minecraft_running": minecraft_running(),
        }
    return set_minecraft_options({MUSIC_OPTION_KEY: 1.0}, mc_dir=mc_dir, log=log)


def restore_minecraft_options(mc_dir=None, log=None) -> dict:
    """Undo our edit by putting the kept backup back (then forgetting it)."""
    path = find_options_file(mc_dir)
    if path is None:
        raise OptionsError("Minecraft's settings folder was not found")
    backup = path.with_name(path.name + OPTIONS_BACKUP_SUFFIX)
    if not backup.is_file():
        raise OptionsError(
            f"there is no backup to restore ({backup.name}) - yt2disc did not "
            "change your settings, or they were already restored"
        )
    try:
        data = backup.read_bytes()
        path.write_bytes(data)
        backup.unlink()
    except OSError as exc:
        raise OptionsError(f"could not restore '{path}' ({exc})") from exc
    if log:
        log(f"Restored {path.name} from {backup.name}")
    values, _order = parse_options(data.decode("utf-8", "replace"))
    return {
        "path": path,
        "backup": None,
        "before": {},
        "after": values,
        "changed": True,
        "minecraft_running": minecraft_running(),
    }


# --------------------------------------------------------------------------
# Shell helpers (used by the GUI, harmless for the CLI)
# --------------------------------------------------------------------------


def open_folder(path) -> bool:
    """Open a directory in the OS file manager."""
    path = Path(path)
    if not path.exists():
        path = path.parent if path.parent.exists() else SCRIPT_DIR
    elif path.is_file():
        path = path.parent
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:
        return False


def reveal_in_folder(path) -> bool:
    """Show one file selected inside the OS file manager."""
    path = Path(path)
    if not path.exists():
        return open_folder(path.parent)
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
        return True
    except Exception:
        return False


def open_url(url: str) -> bool:
    """Open a URL in the default browser (best effort)."""
    try:
        import webbrowser

        return webbrowser.open(url)
    except Exception:
        return False













