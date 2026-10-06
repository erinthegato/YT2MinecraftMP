"""cli.py - command line front-end for yt2disc.

Shares every code path with the GUI (both call into :mod:`core` and
:mod:`player`).

    main.py --cli library add "D:\\Music"
    main.py --cli playlist new Chill --track "D:\\Music\\sweden.ogg"
    main.py --cli play Chill
    main.py --cli play --random
    main.py --cli play --soundtrack records
    main.py --cli soundtrack --list
    main.py --cli mc-music --off

Exit codes: 0 ok, 1 error, 2 usage.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

import core
import player


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Play the music you already have - your own files and Minecraft's "
            "own soundtrack - and keep the game's background music out of the way."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ---- library ------------------------------------------------------
    library = sub.add_parser(
        "library", help="the folders yt2disc remembers as your music library"
    )
    library_sub = library.add_subparsers(dest="action", metavar="<action>")

    library_add = library_sub.add_parser("add", help="remember a folder")
    library_add.add_argument("folder")
    library_remove = library_sub.add_parser("remove", help="forget a folder")
    library_remove.add_argument("folder")
    library_sub.add_parser("list", help="show the remembered folders")
    library_scan = library_sub.add_parser(
        "scan", help="list the audio files in one folder (remembered or not)"
    )
    library_scan.add_argument("folder")
    library_scan.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        help="only look in that folder, not in its sub-folders",
    )

    # ---- playlist -----------------------------------------------------
    playlist = sub.add_parser("playlist", help="create and edit playlists")
    playlist_sub = playlist.add_subparsers(dest="action", metavar="<action>")

    playlist_new = playlist_sub.add_parser("new", help="create a playlist")
    playlist_new.add_argument("name")
    playlist_new.add_argument(
        "--track", action="append", default=[], metavar="PATH", help="add a file"
    )
    playlist_sub.add_parser("list", help="show every playlist")
    playlist_show = playlist_sub.add_parser("show", help="show one playlist")
    playlist_show.add_argument("name")
    playlist_add = playlist_sub.add_parser("add", help="add files to a playlist")
    playlist_add.add_argument("name")
    playlist_add.add_argument("paths", nargs="+", metavar="PATH")
    playlist_add_dir = playlist_sub.add_parser(
        "add-dir", help="add every audio file of a folder"
    )
    playlist_add_dir.add_argument("name")
    playlist_add_dir.add_argument("folder")
    playlist_remove = playlist_sub.add_parser("remove", help="drop one track")
    playlist_remove.add_argument("name")
    playlist_remove.add_argument(
        "selector", help="a 1-based index, a path or a file name"
    )
    playlist_rename = playlist_sub.add_parser("rename", help="rename a playlist")
    playlist_rename.add_argument("name")
    playlist_rename.add_argument("new_name")
    playlist_delete = playlist_sub.add_parser("delete", help="delete a playlist")
    playlist_delete.add_argument("name")

    # ---- play ---------------------------------------------------------
    play = sub.add_parser(
        "play",
        help="play a playlist, your library, a file or Minecraft's soundtrack",
        description=(
            "Blocking: the command returns when the track ends (Ctrl+C stops it "
            "early and switches Minecraft's music back on)."
        ),
    )
    play.add_argument("playlist", nargs="?", default=None, help="playlist to play")
    play.add_argument(
        "--track", action="append", default=[], metavar="PATH", help="a file to play"
    )
    play.add_argument(
        "--soundtrack",
        nargs="?",
        const="",
        default=None,
        metavar="CATEGORY",
        help="play Minecraft's own soundtrack (optionally one category)",
    )
    play.add_argument("--search", default=None, help="filter by title")
    play.add_argument("--random", action="store_true", help="pick at random")
    play.add_argument(
        "--count", type=int, default=1, help="how many tracks (default 1)"
    )
    play.add_argument("--seed", type=int, default=None, help="seed for --random")
    play.add_argument(
        "--no-mute",
        dest="mute",
        action="store_false",
        default=None,
        help="leave Minecraft's music alone while this plays",
    )
    play.add_argument(
        "--mute", dest="mute", action="store_true", help="force muting the game"
    )
    play.add_argument(
        "--dry-run",
        action="store_true",
        help="decode and report, but do not actually make a sound",
    )
    play.add_argument("--mc-dir", metavar="COMOJANG", help="Minecraft data folder")

    # ---- soundtrack ---------------------------------------------------
    soundtrack = sub.add_parser(
        "soundtrack",
        help="Minecraft's own soundtrack, read from the game's files",
    )
    soundtrack.add_argument(
        "--list", action="store_true", dest="list_tracks", help="list the tracks"
    )
    soundtrack.add_argument(
        "--category",
        default=None,
        metavar="CATEGORY",
        help="game / creative / end / nether / records / water / menu",
    )
    soundtrack.add_argument("--search", default=None, help="filter by title")
    soundtrack.add_argument(
        "--add", metavar="PLAYLIST", help="add every match to a playlist"
    )
    soundtrack.add_argument(
        "--content", metavar="CONTENT", help="Minecraft Content folder"
    )

    # ---- mc-music -----------------------------------------------------
    mc_music = sub.add_parser(
        "mc-music", help="switch Minecraft's background music on or off"
    )
    mc_music.add_argument("--off", action="store_true", help="silence the game")
    mc_music.add_argument("--on", action="store_true", help="turn it back on")
    mc_music.add_argument(
        "--status", action="store_true", help="show the current value"
    )
    mc_music.add_argument(
        "--restore", action="store_true", help="put the kept backup back"
    )
    mc_music.add_argument(
        "--records", action="store_true", help="also silence the jukebox channel"
    )
    mc_music.add_argument("--mc-dir", metavar="COMOJANG", help="Minecraft data folder")

    return parser


_FFMPEG_NOISE = (
    "Input #0",
    "Output #0",
    "Stream mapping",
    "Stream #",
    "Duration:",
    "Metadata:",
    "encoder",
    "Press [q]",
    "size=",
    "frame=",
    "video:",
    "At least one output",
    "Guessed Channel",
)


def _make_log():
    """Print progress, but drop ffmpeg's banner and statistics chatter."""
    def log(message: str) -> None:
        text = str(message)
        stripped = text.strip()
        if not stripped:
            return
        if text != text.lstrip():  # the indented detail lines of the banner
            return
        if stripped.startswith(_FFMPEG_NOISE):
            return
        if "%" in stripped:  # the per-second progress lines
            return
        print(text)

    return log


def _require_ffmpeg() -> bool:
    if core.missing_binaries():
        print("ERROR: ffmpeg was not found, so tracks cannot be decoded.", file=sys.stderr)
        print(core.binary_help_text(), file=sys.stderr)
        return False
    return True


def _yes_no(flag) -> str:
    return "yes" if flag else "no"


def _print_tracks(tracks, header=None) -> None:
    if header:
        print(header)
    if not tracks:
        print("  (nothing)")
        return
    width = min(max((len(track["name"]) for track in tracks), default=12), 42)
    for index, track in enumerate(tracks, 1):
        extra = []
        if track.get("duration"):
            extra.append(track["duration_text"])
        elif track.get("size"):
            extra.append(track["size_text"])
        if track.get("vanilla"):
            extra.append(track["category"])
        if not track.get("exists", True):
            extra.append("MISSING")
        print(f"  {index:>3}. {track['name'][:width]:<{width}}  {'  '.join(extra)}")


# --------------------------------------------------------------------------
# library
# --------------------------------------------------------------------------


def _library_listing() -> str:
    roots = core.library_roots()
    lines = [f"{len(roots)} remembered folder(s):"]
    for root in roots:
        lines.append(f"  {root}{'' if Path(root).is_dir() else '   (missing)'}")
    if not roots:
        lines.append('  (none yet - add one with:  main.py library add "D:\\Music")')
    return "\n".join(lines)


def _cmd_library(args) -> int:
    action = getattr(args, "action", None)
    if action == "add":
        report = core.add_library_root(args.folder)
        verb = "Remembered" if report["added"] else "Already in the library:"
        print(f"{verb} {report['folder']}")
        print(f"  {len(report['roots'])} folder(s) remembered")
        return 0
    if action == "remove":
        report = core.remove_library_root(args.folder)
        verb = "Forgot" if report["removed"] else "Was not in the library:"
        print(f"{verb} {report['folder']}")
        return 0
    if action == "scan":
        tracks = core.scan_audio_folder(
            args.folder, recursive=getattr(args, "recursive", True)
        )
        _print_tracks(tracks, f"{len(tracks)} audio file(s) in {args.folder}:")
        return 0
    print(_library_listing())
    if action is None:
        tracks = core.scan_library(log=_make_log())
        if tracks:
            print(f"\n{len(tracks)} track(s) across those folders."
                  "  Play one with:  main.py play --random")
    return 0


# --------------------------------------------------------------------------
# playlist
# --------------------------------------------------------------------------


def _playlist_listing() -> str:
    entries = core.list_playlists()
    if not entries:
        return ('No playlists yet - make one with:  '
                'main.py playlist new "Chill"')
    lines = [f"{len(entries)} playlist(s):"]
    for entry in entries:
        lines.append(
            f"  {entry.get('name', '?'):<28} {len(entry.get('tracks') or []):>4} track(s)"
            f"   id: {entry.get('id')}"
        )
    return "\n".join(lines)


def _cmd_playlist(args) -> int:
    action = getattr(args, "action", None)

    if action == "new":
        tracks = core.resolve_tracks(args.track) if args.track else []
        entry = core.create_playlist(args.name, [track["path"] for track in tracks])
        print(
            f"Created playlist '{entry['name']}' "
            f"({len(entry['tracks'])} track(s), id: {entry['id']})"
        )
        return 0

    if action == "show":
        entry = core.require_playlist(args.name)
        tracks = core.playlist_tracks(args.name)
        print(
            f"'{entry['name']}' - {len(tracks)} track(s), id: {entry['id']}, "
            f"updated {entry.get('updated')}"
        )
        _print_tracks(tracks)
        return 0

    if action in ("add", "add-dir"):
        if action == "add-dir":
            found = core.scan_audio_folder(args.folder)
        else:
            found = core.resolve_tracks(args.paths)
        report = core.add_to_playlist(args.name, [track["path"] for track in found])
        print(
            f"'{report['playlist']['name']}': added {len(report['added'])}, "
            f"already there {len(report['skipped'])}, total {report['total']}"
        )
        for path in report["skipped"]:
            print(f"  already in the list: {Path(path).name}")
        return 0

    if action == "remove":
        report = core.remove_from_playlist(args.name, args.selector)
        print(
            f"Removed '{Path(report['removed']).name}' from "
            f"'{report['playlist']['name']}' ({report['total']} left)"
        )
        return 0

    if action == "rename":
        entry = core.rename_playlist(args.name, args.new_name)
        print(f"Renamed to '{entry['name']}' (id: {entry['id']})")
        return 0

    if action == "delete":
        if not core.delete_playlist(args.name):
            raise core.PlaylistError(f"no playlist called '{args.name}'")
        print(f"Deleted playlist '{args.name}' (the audio files are untouched)")
        return 0

    print(_playlist_listing())
    return 0


# --------------------------------------------------------------------------
# play
# --------------------------------------------------------------------------


def _collect_play_tracks(args) -> list[dict]:
    """Work out what the user asked us to play."""
    if args.track:
        return core.resolve_tracks(args.track)
    if args.soundtrack is not None:
        category = args.soundtrack or None
        tracks = core.list_vanilla_music(category=category, search=args.search)
        if not tracks:
            if category:
                raise core.InputError(f"no soundtrack tracks in category '{category}'")
            raise core.InputError(
                "Minecraft's soundtrack was not found - is Minecraft for Windows "
                "installed? Set YT2DISC_MINECRAFT_CONTENT to its Content folder."
            )
        return tracks
    if args.playlist:
        tracks = core.playlist_tracks(args.playlist, existing_only=True)
        if not tracks:
            raise core.InputError(f"'{args.playlist}' has no playable tracks")
        return tracks
    tracks = core.scan_library(log=_make_log())
    if not tracks:
        raise core.InputError(
            'nothing to play - remember a folder first '
            '(main.py library add "D:\\Music"), or name a file with --track'
        )
    return tracks


def _wait_for_end(engine) -> None:
    """Block until the current track ends (Ctrl+C breaks out)."""
    while engine.playing:
        time.sleep(0.15)


def _cmd_play(args) -> int:
    if not _require_ffmpeg():
        return 1
    tracks = _collect_play_tracks(args)
    engine = player.Player(
        log=_make_log(),
        dry_run=args.dry_run,
        mute_minecraft=args.mute,
        mc_dir=args.mc_dir,
    )
    rng = random.Random(args.seed) if args.seed is not None else None
    count = max(1, args.count)
    played = 0
    current = None
    try:
        for index in range(count):
            if args.random:
                report = engine.play_random(tracks, avoid=current, rng=rng)
            else:
                report = engine.play(
                    core.next_track(tracks, current=current, wrap=True)
                )
            current = report["track"]["path"]
            played += 1
            step = f"[{index + 1}/{count}] " if count > 1 else ""
            print(f"{step}Now playing: {core.describe_track(report['track'])}")
            print(f"          file     : {report['track']['path']}")
            print(f"          backend  : {report['backend']}")
            print(
                "          minecraft: "
                + ("music muted for now" if report["muted"] else "music left alone")
            )
            if not args.dry_run:
                _wait_for_end(engine)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        engine.stop()
    print(f"{played} track(s) played.")
    return 0


# --------------------------------------------------------------------------
# soundtrack
# --------------------------------------------------------------------------


def _cmd_soundtrack(args) -> int:
    content = args.content
    wants_list = bool(args.list_tracks or args.category or args.search)

    if args.add:
        tracks = core.list_vanilla_music(
            content=content, category=args.category, search=args.search
        )
        if not tracks:
            raise core.InputError("no soundtrack tracks matched")
        report = core.add_to_playlist(args.add, [track["path"] for track in tracks])
        print(
            f"'{report['playlist']['name']}': added {len(report['added'])}, "
            f"already there {len(report['skipped'])}, total {report['total']}"
        )
        return 0

    if wants_list:
        tracks = core.list_vanilla_music(
            content=content, category=args.category, search=args.search
        )
        if not tracks:
            raise core.InputError(
                "nothing matched - check --category, or set "
                "YT2DISC_MINECRAFT_CONTENT to your Minecraft Content folder"
            )
        _print_tracks(tracks, f"{len(tracks)} soundtrack track(s):")
        return 0

    root = core.vanilla_music_root(content)
    if root is None:
        print("Minecraft's soundtrack was not found.", file=sys.stderr)
        print(
            "Searched:\n  "
            + "\n  ".join(str(path) for path in core.minecraft_content_roots()[:6])
            + "\nSet YT2DISC_MINECRAFT_CONTENT to the folder that holds "
            "'data\\resource_packs'.",
            file=sys.stderr,
        )
        return 1
    print(f"Minecraft soundtrack: {root}")
    for entry in core.soundtrack_summary():
        print(f"  {entry['category']:<10} {entry['count']:>4} track(s)")
    print('\nPlay one with:  main.py play --soundtrack records')
    return 0


# --------------------------------------------------------------------------
# mc-music
# --------------------------------------------------------------------------


def _cmd_mc_music(args) -> int:
    log = _make_log()

    if args.restore:
        report = core.restore_minecraft_options(args.mc_dir, log=log)
        print(f"Restored {report['path']} from the backup")
        return 0

    if args.on:
        report = core.unmute_minecraft_music(args.mc_dir, log=log)
        if report["path"] is None:
            print("Minecraft's options.txt was not found.", file=sys.stderr)
            return 1
        print(f"audio_music = {report['after'].get(core.MUSIC_OPTION_KEY)}")
        print(f"  in {report['path']}")
        return 0

    if args.off:
        report = core.mute_minecraft_music(
            args.mc_dir, log=log, also_records=args.records
        )
        print(f"audio_music = {report['after'].get(core.MUSIC_OPTION_KEY)}")
        if args.records:
            print(f"audio_record = {report['after'].get(core.RECORD_OPTION_KEY)}")
        print(f"  in {report['path']}")
        if report["backup"]:
            print(f"  previous settings kept in {report['backup'].name}")
        print("\nUndo any time with:  main.py mc-music --on")
        if report["minecraft_running"]:
            print(
                "\nNOTE: Minecraft is running. The game rewrites options.txt when "
                "it exits,\n      so close it for this to stick.",
                file=sys.stderr,
            )
        return 0

    report = core.read_minecraft_options(args.mc_dir)
    if not report["exists"]:
        print("Minecraft's options.txt was not found - run the game once first.")
        return 1
    print(f"{report['path']}")
    state = "OFF" if report["music_muted"] else "on"
    print(f"  audio_music  = {report['music']}   ({state})")
    print(f"  audio_record = {report['record']}")
    return 0


def run(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))

    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2

    try:
        if args.command == "library":
            return _cmd_library(args)
        if args.command == "playlist":
            return _cmd_playlist(args)
        if args.command == "play":
            return _cmd_play(args)
        if args.command == "soundtrack":
            return _cmd_soundtrack(args)
        if args.command == "mc-music":
            return _cmd_mc_music(args)
    except core.BinaryMissingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(core.binary_help_text(), file=sys.stderr)
        return 1
    except core.YT2DiscError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted by user.", file=sys.stderr)
        return 1

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())





