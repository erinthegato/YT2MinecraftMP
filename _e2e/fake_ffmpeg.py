"""Fake ffmpeg used by _e2e/run_e2e.py - no network access, no real media.

Probe mode : ``ffmpeg -hide_banner -i FILE``     (prints a banner, exit 1)
Decode mode: ``ffmpeg -y ... -i FILE ... OUT``   (writes a real WAV, exit 0)

The WAV it writes is a genuine (tiny, silent) RIFF file, so the whole decode
path - size check, rename, cache lookup - behaves exactly like it does with the
real ffmpeg.  Nothing is ever played: the test runs with YT2DISC_FAKE_PLAYER=1.
"""

import os
import sys
import wave


def probe(argv) -> int:
    name = os.path.basename(argv[-1]) if argv else "unknown"
    sys.stderr.write(
        "Input #0, ogg, from '%s':\n"
        "  Duration: 00:04:07.50, start: 0.000000, bitrate: 96 kb/s\n"
        "    Stream #0:0: Audio: vorbis, 44100 Hz, stereo, fltp, 96 kb/s\n" % name
    )
    return 1


def convert(argv) -> int:
    destination = argv[-1]
    folder = os.path.dirname(destination)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with wave.open(destination, "w") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x00\x00\x00" * 2205)  # 50 ms of silence
    print("size=     172kB time=00:00:04.07 bitrate= 345kbits/s speed= 100x")
    return 0


def main(argv) -> int:
    # Every decode invocation carries -y; the probe does not.
    if "-y" in argv:
        return convert(argv)
    return probe(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
