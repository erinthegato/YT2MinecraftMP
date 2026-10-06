"""player.py - local audio playback for yt2disc.

The engine in :mod:`core` knows *what* a track is; this module knows how to
make the machine play it, and how to keep Minecraft quiet while it does.

The backend is deliberately small and swappable:

* :class:`WinsoundBackend` - the Windows standard library player.  It only
  accepts WAV, which is why every track is decoded once by ffmpeg into
  ``cache/`` first.  Playback is blocking on a worker thread, so the end of a
  track is known exactly and ``stop()`` can cut it off instantly.
* :class:`DryRunBackend` - pretends to play.  Used by ``play --dry-run`` and by
  the end-to-end test, so the whole chain (decode, mute, advance) is verified
  without making a sound.
* :class:`NullBackend` - anything that is not Windows.

There is one honest limitation: the standard library player cannot pause, seek
or change the volume of a running track.  Stop + play is what it offers, and
that is what the UI offers too.
"""

from __future__ import annotations

import atexit
import os
import threading

import core


class PlayerError(core.YT2DiscError):
    """Playback could not be started."""


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


class Backend:
    """Interface every backend implements."""

    name = "none"

    def __init__(self):
        self._playing = False

    @property
    def playing(self) -> bool:
        return self._playing

    def play(self, wav_path, done) -> None:
        """Start playback; call ``done()`` exactly once when it ends."""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class DryRunBackend(Backend):
    """Play nothing, instantly - for tests and ``--dry-run``."""

    name = "dry-run"

    def play(self, wav_path, done) -> None:
        self._playing = True
        try:
            done()
        finally:
            self._playing = False

    def stop(self) -> None:
        self._playing = False


class NullBackend(Backend):
    """No audio output available on this platform."""

    name = "silent"

    def play(self, wav_path, done) -> None:
        raise PlayerError(
            "this build of yt2disc can only play audio on Windows "
            "(the standard library player is Windows only)"
        )

    def stop(self) -> None:
        self._playing = False


class WinsoundBackend(Backend):
    """Windows' built-in WAV player, on a worker thread."""

    name = "winsound"

    def __init__(self):
        import winsound  # only exists on Windows

        super().__init__()
        self._winsound = winsound
        self._thread: threading.Thread | None = None

    def play(self, wav_path, done) -> None:
        self._playing = True

        def run():
            try:
                self._winsound.PlaySound(
                    str(wav_path),
                    self._winsound.SND_FILENAME | self._winsound.SND_NODEFAULT,
                )
            except RuntimeError:  # unreadable/again-purged file: treated as done
                pass
            finally:
                self._playing = False
                done()

        self._thread = threading.Thread(target=run, name="yt2disc-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._playing:
            try:
                self._winsound.PlaySound(None, self._winsound.SND_PURGE)
            except RuntimeError:  # pragma: no cover - nothing to purge
                pass
        self._playing = False


def pick_backend(force_dry: bool = False) -> Backend:
    """The best backend for this machine (or a dry run when asked)."""
    if force_dry or os.environ.get(core.FAKE_PLAYER_ENV):
        return DryRunBackend()
    if os.name == "nt":
        try:
            return WinsoundBackend()
        except Exception:  # pragma: no cover - no winsound available
            return NullBackend()
    return NullBackend()


# --------------------------------------------------------------------------
# The player
# --------------------------------------------------------------------------


class Player:
    """Plays one track at a time and can walk a queue.

    ``on_event(name, **payload)`` is called with ``preparing``, ``start``,
    ``finished`` and ``stopped`` so a UI can follow along.  The callback may run
    on the audio thread - a GUI must marshal it back to its own thread.
    """

    def __init__(
        self,
        backend=None,
        log=None,
        on_event=None,
        dry_run: bool = False,
        mute_minecraft=None,
        mc_dir=None,
    ):
        self.backend = backend or pick_backend(dry_run)
        self.log = log
        self.on_event = on_event
        self.mc_dir = mc_dir
        self.mute_minecraft = (
            core.mute_preference() if mute_minecraft is None else bool(mute_minecraft)
        )
        self.now_playing: dict | None = None
        self.queue: list[dict] = []
        self.autoplay = False
        self._muted = False
        self._previous_music = None
        self._lock = threading.RLock()
        atexit.register(self.shutdown)

    # ---- small helpers ---------------------------------------------------

    @property
    def playing(self) -> bool:
        return self.backend.playing

    @property
    def muted(self) -> bool:
        return self._muted

    def _say(self, message) -> None:
        if self.log:
            self.log(message)

    def _emit(self, event: str, **payload) -> None:
        if not self.on_event:
            return
        try:
            self.on_event(event, **payload)
        except Exception:  # pragma: no cover - a broken UI must not stop playback
            pass

    # ---- Minecraft's own music ------------------------------------------

    def silence_minecraft(self) -> bool:
        """Switch the game's background music off, remembering the old value."""
        if not self.mute_minecraft or self._muted:
            return self._muted
        current = core.read_minecraft_options(self.mc_dir)
        previous = current.get("music") if current.get("exists") else None
        try:
            report = core.mute_minecraft_music(mc_dir=self.mc_dir, log=self.log)
        except core.YT2DiscError as exc:
            self._say(f"could not mute Minecraft's music: {exc}")
            return False
        if report.get("changed"):
            self._muted = True
            self._previous_music = previous
            self._say(
                "Minecraft's background music switched off while yt2disc plays "
                "(audio_music:0 in options.txt)"
            )
        elif report.get("path"):
            self._muted = True  # already 0 - nothing to restore later
            self._previous_music = 0.0
        return self._muted

    def restore_minecraft(self) -> bool:
        """Put the game's music slider back the way we found it."""
        if not self._muted:
            return False
        self._muted = False
        value = self._previous_music if self._previous_music is not None else 1.0
        self._previous_music = None
        try:
            report = core.set_minecraft_options(
                {core.MUSIC_OPTION_KEY: value}, mc_dir=self.mc_dir, log=self.log
            )
        except core.YT2DiscError as exc:
            self._say(f"could not restore Minecraft's music: {exc}")
            return False
        if report.get("changed"):
            self._say(f"Minecraft's background music switched back to {value:g}")
        return True


    # ---- playing ---------------------------------------------------------

    def prepare(self, track) -> dict:
        """Probe a track and decode it into a playable WAV."""
        if not isinstance(track, dict):
            track = core.track_from_path(track)
        if not track.get("exists", True):
            raise PlayerError(f"'{track.get('path')}' is gone - re-add it or drop it")
        self._emit("preparing", track=track)
        duration = core.probe_duration(track["path"])
        if duration:
            track["duration"] = duration
            track["duration_text"] = core.fmt_duration(duration)
        track["playable"] = str(core.prepare_playable(track["path"], log=self.log))
        return track

    def play(self, track, silence=None) -> dict:
        """Decode and start ``track``; returns a small report."""
        with self._lock:
            self.backend.stop()
            prepared = self.prepare(track)
            self.now_playing = prepared
            want_silence = self.mute_minecraft if silence is None else bool(silence)
            if want_silence:
                self.silence_minecraft()
            # Read the flag *before* playback starts: a backend that finishes
            # instantly (dry run) restores the music again inside play().
            muted = self._muted
            core.remember_played(prepared)
            self._emit("start", track=prepared)
            self.backend.play(prepared["playable"], self._finished)
            return {
                "track": prepared,
                "backend": self.backend.name,
                "muted": muted,
            }

    def play_random(self, tracks, avoid=None, rng=None, silence=None) -> dict:
        """Play a random track - never the one that is already running."""
        avoid = avoid or (self.now_playing or {}).get("path")
        track = core.pick_random_track(tracks, avoid=avoid, rng=rng)
        report = self.play(track, silence=silence)
        report["random"] = True
        return report

    def set_queue(self, tracks, autoplay: bool = True) -> list[dict]:
        """Remember a running order; ``autoplay`` walks it as tracks end.

        A dry run never auto-advances: its backend finishes instantly, so
        walking the queue would loop forever instead of playing one track.
        """
        self.queue = [track for track in tracks or [] if isinstance(track, dict)]
        self.autoplay = (
            bool(autoplay)
            and len(self.queue) > 1
            and not isinstance(self.backend, DryRunBackend)
        )
        return self.queue

    def play_queue(self, tracks, start=None, silence=None, autoplay=True) -> dict:
        """Start a queue at ``start`` (the first track by default)."""
        self.set_queue(tracks, autoplay=autoplay)
        if not self.queue:
            raise PlayerError("nothing to play")
        first = start or self.queue[0]
        return self.play(first, silence=silence)

    def play_playlist(self, name, shuffle: bool = False, silence=None) -> dict:
        """Play a playlist: in order by default, one random track when shuffled."""
        tracks = core.playlist_tracks(name, existing_only=True)
        if not tracks:
            raise PlayerError(f"'{name}' has no playable tracks - add some files first")
        if shuffle:
            return self.play_random(tracks, silence=silence)
        return self.play_queue(tracks, silence=silence)

    def play_next(self, silence=None) -> dict | None:
        """Jump to the next track of the queue (wrapping) - ``None`` if empty."""
        following = self._next_in_queue(self.now_playing)
        if following is None:
            return None
        return self.play(following, silence=silence)

    def stop(self, restore: bool = True) -> None:
        """Stop playback, and give Minecraft its music back."""
        with self._lock:
            self.backend.stop()
            self.now_playing = None
            self._emit("stopped")
            if restore:
                self.restore_minecraft()

    def _next_in_queue(self, current) -> dict | None:
        """The next track of the queue, wrapping around when autoplaying."""
        if not self.queue:
            return None
        paths = [track.get("path") for track in self.queue]
        position = current.get("path") if isinstance(current, dict) else None
        index = paths.index(position) + 1 if position in paths else 0
        if index >= len(self.queue):
            if not self.autoplay or len(self.queue) < 2:
                return None
            index = 0
        return self.queue[index]

    def _finished(self, *_args) -> None:
        """Called (possibly on the audio thread) when a track runs out."""
        track = self.now_playing
        self._emit("finished", track=track)
        if self.autoplay and self.queue:
            following = self._next_in_queue(track)
            if following is not None:
                try:
                    self.play(following)
                    return
                except core.YT2DiscError as exc:
                    self._say(f"could not play the next track: {exc}")
        self.now_playing = None
        self.restore_minecraft()

    def shutdown(self) -> None:
        """Quietly give Minecraft its music back (registered with atexit)."""
        try:
            self.backend.stop()
        except Exception:  # pragma: no cover - defensive
            pass
        self.now_playing = None
        self.restore_minecraft()


