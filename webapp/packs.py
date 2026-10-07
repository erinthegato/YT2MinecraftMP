"""packs.py - build a Minecraft Bedrock addon that plays your music in-game.

The hosted half of yt2disc otherwise hands back a bare audio file.  This module
turns that audio into the thing the *game* can read: a Bedrock addon - a
``.mcaddon``, which is just a zip - holding two packs,

    <name>_RP/   a resource pack: the sound, and the definition that names it
    <name>_BP/   a behavior pack:  a small script that draws the in-game menu

Dropping the ``.mcaddon`` into Minecraft gives the player a music player
*inside* the game: they run ``/yt2disc:music``, a form lists the songs, and
picking one plays it.  That is the whole point of yt2disc - it is a player, not
a stack of jukebox discs.

Everything here is the standard library - ``json``, ``uuid``, ``zipfile``,
``zlib`` and ``struct`` - because the same code has to run in a browser, on
Pyodide, where nothing else is installed.  ffmpeg is *not* used here: by the
time this is called the audio is already an Ogg the resource pack can read.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import uuid
import zipfile
import zlib
from pathlib import Path

# Same trick as converter.py: ``core.py`` lives one folder up and is not always
# importable when a host starts us from somewhere else.
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import core  # noqa: E402  (import after the sys.path fix above)

# --------------------------------------------------------------------------
# The pack, and the game versions it targets
# --------------------------------------------------------------------------
#
# These are the numbers that decide whether the game will load the addon, so
# they are here in one place rather than buried in a template.
#
# * ``@minecraft/server`` 2.x and ``@minecraft/server-ui`` 2.x are the current
#   *stable* script modules - the form UI no longer needs the Beta APIs
#   experiment.  Lower these two strings if the addon has to run on an older
#   game (1.x modules need "Beta APIs" on).
# * The way in is a custom slash command, which is a stable API too, so it
#   needs no experiment either: it opens the menu in an ordinary world, be it
#   single-player, offline, or a server.  The script also registers a
#   ``/scriptevent`` fallback for a game old enough to predate custom commands.

MANIFEST_FORMAT_VERSION = 2
MIN_ENGINE_VERSION = [1, 21, 0]
PACK_VERSION = [1, 0, 0]
SOUND_FORMAT_VERSION = "1.14.0"
SERVER_MODULE_VERSION = "2.0.0"
SERVER_UI_MODULE_VERSION = "2.0.0"

NAMESPACE = "yt2disc"
# A custom command's name has to carry a namespace, so the player types the
# whole thing: "/yt2disc:music".
COMMAND_NAME = f"{NAMESPACE}:music"
COMMAND_DESCRIPTION = "Open the yt2disc music player"
MENU_SCRIPT_EVENT = f"{NAMESPACE}:menu"
DEFAULT_PACK_NAME = "yt2disc music player"

# The audio is Ogg Vorbis, which Bedrock reads; ``stream`` keeps a long track on
# disk instead of loading all of it into memory at once.
SOUND_CATEGORY = "music"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

# A pack's UUIDs have to be *stable*: the game keys a pack by that number, so
# two builds that differ only in random UUIDs are two different packs - an
# update installs *beside* the old one instead of over it.  A version-5 UUID
# hashed from the namespace and the pack's role is the same on every build, on
# every machine and in the browser, which is what an addon actually wants.
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, f"{NAMESPACE}.minecraft.addon")


def _stable_uuid(role: str, pack_name: str = DEFAULT_PACK_NAME) -> str:
    """A deterministic UUID for one named part of the pack (header, module...)."""
    seed = f"{NAMESPACE}:{core.slugify(pack_name)}:{role}"
    return str(uuid.uuid5(_UUID_NAMESPACE, seed))


def _js_string(value) -> str:
    """A Python string as a double-quoted JavaScript string literal."""
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return '"' + text + '"'


def pack_folder_names(pack_name: str = DEFAULT_PACK_NAME) -> tuple[str, str]:
    """The two folder names inside the addon: ``(<name>_RP, <name>_BP)``.

    Public so the tests and the docs can name the same folders the builder
    does, instead of each restating the ``_RP``/``_BP`` spelling.
    """
    stem = core.slugify(pack_name) or NAMESPACE
    return f"{stem}_RP", f"{stem}_BP"


# --------------------------------------------------------------------------
# The icon
# --------------------------------------------------------------------------
#
# A pack icon is optional, but a grey blank square next to every other pack is
# a poor first impression.  Pillow is not available in the browser, so the PNG
# is drawn and encoded here with ``zlib`` and ``struct`` alone.

ICON_SIZE = 128
_ICON_BACKGROUND = (24, 28, 34)
_ICON_DISC = (46, 160, 90)
_ICON_NOTE = (240, 244, 248)


def _inside_note(x: float, y: float, width: float, height: float) -> bool:
    """True where a simple music note sits, in unit square coordinates."""
    nx, ny = x / width, y / height
    # the head - a filled ellipse low and left
    if ((nx - 0.42) / 0.17) ** 2 + ((ny - 0.64) / 0.13) ** 2 <= 1.0:
        return True
    # the stem
    if 0.55 <= nx <= 0.605 and 0.28 <= ny <= 0.66:
        return True
    # the flag, sloping right and down from the top of the stem
    if 0.60 <= nx <= 0.80:
        top = 0.28 + (nx - 0.60) * 0.30
        if top <= ny <= top + 0.10:
            return True
    return False


def _png(width: int, height: int, pixels: bytes) -> bytes:
    """Encode a raw RGB pixel buffer as a minimal, filter-free PNG."""
    row_bytes = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 ("None") for every scanline
        raw += pixels[y * row_bytes : (y + 1) * row_bytes]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def pack_icon(size: int = ICON_SIZE) -> bytes:
    """Draw the pack's icon: a music note on a dark disc."""
    centre = size / 2.0
    radius = size * 0.46
    pixels = bytearray()
    for y in range(size):
        for x in range(size):
            on_disc = (x - centre + 0.5) ** 2 + (y - centre + 0.5) ** 2 <= radius * radius
            if on_disc and _inside_note(x + 0.5, y + 0.5, size, size):
                pixels += bytes(_ICON_NOTE)
            elif on_disc:
                pixels += bytes(_ICON_DISC)
            else:
                pixels += bytes(_ICON_BACKGROUND)
    return _png(size, size, bytes(pixels))



# --------------------------------------------------------------------------
# The manifests and the sound definitions
# --------------------------------------------------------------------------

def resource_manifest(pack_name: str, description: str) -> dict:
    """The resource pack's ``manifest.json`` - where the sound lives."""
    return {
        "format_version": MANIFEST_FORMAT_VERSION,
        "header": {
            "name": pack_name,
            "description": description,
            "uuid": _stable_uuid("rp/header", pack_name),
            "version": list(PACK_VERSION),
            "min_engine_version": list(MIN_ENGINE_VERSION),
        },
        "modules": [
            {
                "type": "resources",
                "description": f"{pack_name} sounds",
                "uuid": _stable_uuid("rp/module", pack_name),
                "version": list(PACK_VERSION),
            }
        ],
    }


def behavior_manifest(pack_name: str, description: str) -> dict:
    """The behavior pack's ``manifest.json`` - where the menu script lives.

    The ``script`` module plus the two ``@minecraft/server`` dependencies are
    what let a pack draw a form; without them Minecraft treats the ``.js`` file
    as decoration and never runs it.
    """
    return {
        "format_version": MANIFEST_FORMAT_VERSION,
        "header": {
            "name": f"{pack_name} (behavior)",
            "description": description,
            "uuid": _stable_uuid("bp/header", pack_name),
            "version": list(PACK_VERSION),
            "min_engine_version": list(MIN_ENGINE_VERSION),
        },
        "modules": [
            {
                "type": "data",
                "description": f"{pack_name} behavior",
                "uuid": _stable_uuid("bp/data", pack_name),
                "version": list(PACK_VERSION),
            },
            {
                "type": "script",
                "language": "javascript",
                "entry": "scripts/main.js",
                "description": f"{pack_name} menu script",
                "uuid": _stable_uuid("bp/script", pack_name),
                "version": list(PACK_VERSION),
            },
        ],
        "dependencies": [
            {"module_name": "@minecraft/server", "version": SERVER_MODULE_VERSION},
            {
                "module_name": "@minecraft/server-ui",
                "version": SERVER_UI_MODULE_VERSION,
            },
        ],
    }


def sound_definitions(tracks: list[dict]) -> dict:
    """One entry per track: the id the script plays -> the Ogg in this pack."""
    definitions = {}
    for track in tracks:
        definitions[track["id"]] = {
            "category": SOUND_CATEGORY,
            "sounds": [{"name": f"sounds/{track['slug']}", "stream": True}],
        }
    return {
        "format_version": SOUND_FORMAT_VERSION,
        "sound_definitions": definitions,
    }



# --------------------------------------------------------------------------
# The script - the in-game music player itself
# --------------------------------------------------------------------------

MAIN_JS = """\
// yt2disc - an in-game music player.
//
// Run "/yt2disc:music" to open the menu.  The songs come from the resource
// pack beside this behavior pack (sounds/sound_definitions.json), and playing
// one is a playSound call to the matching sound id.

import { system, CommandPermissionLevel } from "@minecraft/server";
import { ActionFormData } from "@minecraft/server-ui";
import { TRACKS } from "./tracks.js";

const COMMAND = {{COMMAND}};
const MENU_EVENT = {{MENU_EVENT}};

function note(message) {
  try {
    console.warn("[yt2disc] " + message);
  } catch (ignored) {
    // a console that will not take a line is not worth crashing over
  }
}

function playTrack(player, track) {
  try {
    // No location given, so the sound is not tied to a point in the world and
    // does not fade as the player wanders away from it.
    player.playSound(track.id, { volume: 1.0, pitch: 1.0 });
    player.sendMessage("\\u00a7aNow playing: \\u00a7f" + track.name);
  } catch (error) {
    player.sendMessage("\\u00a7cCould not play " + track.name + ": " + error);
  }
}

function stopMusic(player) {
  try {
    player.stopAllSounds();
    player.sendMessage("\\u00a77Music stopped.");
  } catch (error) {
    note("stopAllSounds failed: " + error);
  }
}

function showMenu(player) {
  const form = new ActionFormData()
    .title("Music player")
    .body(TRACKS.length + " song(s) - pick one to play.");
  for (const track of TRACKS) {
    form.button(track.name);
  }
  form.button("Stop the music");

  form
    .show(player)
    .then((response) => {
      if (response.canceled) {
        return;
      }
      if (response.selection < TRACKS.length) {
        playTrack(player, TRACKS[response.selection]);
      } else {
        stopMusic(player);
      }
    })
    .catch((error) => note("the menu failed to open: " + error));
}

// The command.  A custom command is a stable API, so "/yt2disc:music" needs no
// experiment and no server - it opens the menu in any ordinary world, a
// single-player one included.  cheatsRequired is off so a plain player, not
// just an operator, can use it.
try {
  system.beforeEvents.startup.subscribe((event) => {
    event.customCommandRegistry.registerCommand(
      {
        name: COMMAND,
        description: {{COMMAND_DESCRIPTION}},
        permissionLevel: CommandPermissionLevel.Any,
        cheatsRequired: false,
      },
      (origin) => {
        const player = origin.sourceEntity;
        if (!player) {
          return;
        }
        // A form cannot be opened from inside the command itself, so hop to
        // the next tick.
        system.run(() => showMenu(player));
      }
    );
  });
} catch (error) {
  note("the /" + COMMAND + " command is unavailable: " + error);
}

// A fallback for a game too old for custom commands:
//     /scriptevent {{MENU_EVENT_RAW}}
try {
  system.afterEvents.scriptEventReceive.subscribe((event) => {
    if (event.id !== MENU_EVENT) {
      return;
    }
    const player = event.sourceEntity;
    if (player) {
      system.run(() => showMenu(player));
    }
  });
} catch (error) {
  note("scriptevent trigger unavailable: " + error);
}

note("music player ready - " + TRACKS.length + " track(s)");
"""


def main_script() -> str:
    """The player script, with this pack's command and fallback filled in."""
    return (
        MAIN_JS.replace("{{COMMAND}}", _js_string(COMMAND_NAME))
        .replace("{{COMMAND_DESCRIPTION}}", _js_string(COMMAND_DESCRIPTION))
        .replace("{{MENU_EVENT_RAW}}", MENU_SCRIPT_EVENT)
        .replace("{{MENU_EVENT}}", _js_string(MENU_SCRIPT_EVENT))
    )


def tracks_script(tracks: list[dict]) -> str:
    """The song list the player script imports, so the data stays data."""
    lines = [
        "// Generated by yt2disc - the songs this pack can play.",
        "export const TRACKS = [",
    ]
    for track in tracks:
        lines.append(
            "  { id: %s, name: %s },"
            % (_js_string(track["id"]), _js_string(track["name"]))
        )
    lines.append("];")
    lines.append("")
    return "\n".join(lines)



# --------------------------------------------------------------------------
# Assembling the addon
# --------------------------------------------------------------------------

def _clean_tracks(tracks, log=None) -> list[dict]:
    """Validate the request into the rows the pack is built from.

    Every track needs a readable audio file, a unique slug and a name.  A blank
    name borrows the slug; a duplicate slug is nudged apart rather than lost.
    """
    cleaned: list[dict] = []
    used: set[str] = set()
    for entry in tracks or []:
        entry = entry if isinstance(entry, dict) else {}
        audio = entry.get("audio")
        audio = Path(str(audio)) if audio else None
        if audio is None or not audio.is_file():
            if log:
                log(f"pack    : skipping '{entry.get('name') or '?'}' - no audio")
            continue
        slug = core.slugify(entry.get("slug") or entry.get("name") or audio.stem)
        slug = slug.strip("_") or "track"
        base, counter = slug, 2
        while slug in used:
            slug = f"{base}_{counter}"
            counter += 1
        used.add(slug)
        name = str(entry.get("name") or "").strip() or slug.replace("_", " ")
        cleaned.append(
            {
                "slug": slug,
                "name": name,
                "id": f"{NAMESPACE}.{slug}",
                "audio": audio,
                "duration": entry.get("duration"),
            }
        )
    return cleaned


def build_addon(
    destination,
    tracks,
    log=None,
    pack_name: str = DEFAULT_PACK_NAME,
    description: str = "",
) -> Path:
    """Write a ``.mcaddon`` holding the resource pack and the behavior pack.

    ``tracks`` is a list of ``{"slug", "name", "audio", "duration"}`` rows; the
    audio is expected to be Ogg already.  Returns ``destination``.
    """
    destination = Path(str(destination))
    cleaned = _clean_tracks(tracks, log=log)
    if not cleaned:
        raise core.InputError("there is no audio to put in the pack")

    description = str(description or "").strip() or (
        f"{len(cleaned)} song(s) for the in-game music player.  "
        f"Run /{COMMAND_NAME} to open the menu."
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    icon = pack_icon()
    rp, bp = pack_folder_names(pack_name)

    # Written beside the target and moved into place, so a half-written addon
    # can never be handed to a download link.
    tmp = destination.with_name(destination.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            f"{rp}/manifest.json",
            json.dumps(resource_manifest(pack_name, description), indent=2),
        )
        bundle.writestr(
            f"{rp}/sounds/sound_definitions.json",
            json.dumps(sound_definitions(cleaned), indent=2),
        )
        for track in cleaned:
            bundle.write(track["audio"], f"{rp}/sounds/{track['slug']}.ogg")
        bundle.writestr(f"{rp}/pack_icon.png", icon)

        bundle.writestr(
            f"{bp}/manifest.json",
            json.dumps(behavior_manifest(pack_name, description), indent=2),
        )
        bundle.writestr(f"{bp}/scripts/main.js", main_script())
        bundle.writestr(f"{bp}/scripts/tracks.js", tracks_script(cleaned))
        bundle.writestr(f"{bp}/pack_icon.png", icon)

    os.replace(tmp, destination)
    if log:
        log(
            f"pack    : {destination.name} - {len(cleaned)} song(s), "
            f"RP + BP, run /{COMMAND_NAME} to open it"
        )
    return destination

