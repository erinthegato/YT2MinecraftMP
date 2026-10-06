---
title: yt2minecraftdisc converter
emoji: 🎵
colorFrom: green
colorTo: blue
sdk: static
app_file: index.html
pinned: false
---

# yt2disc — change report

**Commit** `5501d50` — `feat(packs): open the player with the /yt2disc:music custom command`
**Pushed to** `origin/main` — `github.com/joshyoon27-netizen/YT2MinecraftDISC`
**Working tree** clean; nothing ahead of or behind the remote.

This file is the report for that change. The project's own documentation - both
halves, every deploy target, every smoke test - now lives in
[`docs/PROJECT-README.md`](docs/PROJECT-README.md).

## Changes

- **The player opens with a command, not a chat message.** The behavior pack now
  registers `yt2disc:music` through `customCommandRegistry` at startup, so
  `/yt2disc:music` is a real command in the command list - tab-completable,
  permission-gated by the game, and no longer dependent on a script watching the
  chat feed for `!music`. The old chat trigger is gone.
- **`/scriptevent yt2disc:menu` is kept as the fallback.** It opens the same form,
  so a game too old for custom commands still has a way in. Both entry points run
  the same callback.
- **Pack generation has one implementation.** `webapp/packs.py` is the single
  source for both packs and their icons; `build_exe.py` now imports it instead of
  carrying its own copy, so a change to a manifest, a UUID, or an icon lands in
  the packaged build and the converted `.mcaddon` at the same time.
- **Duration is read from the source audio.** An `.mcaddon` is a zip archive, so
  probing the finished addon with ffprobe found no audio stream and the reported
  length was wrong or missing. The duration now comes from the source audio,
  before packaging.
- **The stale `build/` output path is gone** - removed from `yt2disc.spec`,
  `build_exe.py`, and the end-to-end expectations, so the packaged layout the
  tests check for is the layout that ships.

## What the Code Does

| Piece | Role |
| --- | --- |
| behavior pack, startup | registers `yt2disc:music` with `customCommandRegistry`; the callback opens the song picker |
| behavior pack, `scriptEventReceive` | `yt2disc:menu` calls the same callback, for versions without custom commands |
| resource pack | carries the converted song; the icon comes from the same generator as the behavior pack's |
| `webapp/packs.py` | builds both packs, writes the manifests with stable UUIDs, renders the icons |
| `build_exe.py` | imports `packs.py` rather than restating it |
| `webapp/converter.py` | takes the duration from the source audio, never from the finished archive |

Turning the packs on for a world needs no experiment enabled and works offline,
because the way in is a custom command - the same reason the form API it opens
needed none.

## Next Steps

1. Verify `/yt2disc:music` on the minimum supported game version, and document
   the `/scriptevent` fallback in the README.
2. Add pack-generation regression tests: zip validity, stable UUIDs, title and
   duration taken from the source.
3. Harden the ffprobe duration fallback and surface a clear job error when it
   fails.
4. Add upload size/type validation and a job timeout.
5. CI: run `webapp/_selftest.py` and `_e2e/run_e2e.py` on push, and pin the
   ffmpeg check.
6. DX: centralize pack paths and constants across `webapp/packs.py`,
   `build_exe.py`, and `yt2disc.spec`.
