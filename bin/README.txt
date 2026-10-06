yt2disc helper binaries
=======================

This folder is intentionally empty in the repository.  Drop the two
portable helper programs here before running yt2disc:

  bin/yt-dlp.exe   ->  https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe
  bin/ffmpeg.exe   ->  https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
                       (alternative builds: https://github.com/BtbN/FFmpeg-Builds/releases)

Unzip the ffmpeg archive and copy ffmpeg.exe from *its* bin/ folder into
this bin/ folder.

Linux / macOS: place executables named `yt-dlp` and `ffmpeg` here and run
`chmod +x bin/yt-dlp bin/ffmpeg`.  yt2disc adds a missing execute bit itself,
so the `chmod` is only needed if the folder is not writable.

Both programs are also searched on your PATH, and the environment variables
YT2DISC_YT_DLP / YT2DISC_FFMPEG can point at custom locations.  On Linux and
macOS a binary on PATH wins over this folder, because the copy a package
manager installed (`sudo apt install ffmpeg`) is the one that gets security
updates; on Windows this folder is still tried first, so the portable build
stays self-contained.
