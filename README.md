---
title: yt2minecraftdisc converter
emoji: 🎵
colorFrom: green
colorTo: blue
sdk: static
app_file: index.html
pinned: false
---

# yt2disc

Two halves of one idea, split on purpose:

| Half | Where it runs | What it does |
| --- | --- | --- |
| the local add-on | your PC - `main.py`, `gui.py`, `cli.py`, `player.py` | plays the music you already own: your own audio files, and the soundtrack that ships inside Minecraft for Windows. It builds playlists and can mute Minecraft's own background music while it plays, so a custom song is never fighting the game |
| `app.py` | a server, or the visitor's own browser | the page a visitor uses: a file goes in, a file comes out. Gradio draws it - the form, the progress bar, the download link |
| `webapp/` | behind that page | the conversion engine: what to run ffmpeg with, how far along it is, what to call the result. In a browser, `browser_ffmpeg.py` is the ffmpeg - WebAssembly instead of a program |

Converting is the part worth offloading to a server - it wants ffmpeg and it
takes a while - while playing is local, because it needs your speakers, your
files and your game. The two halves meet at exactly one place: `core.py`, from
which the web half borrows the binary lookup and nothing else.

## Run the player

```powershell
py -3 main.py                       # desktop GUI (no arguments)
py -3 main.py --help                # the same jobs from the command line
```

Drop `ffmpeg.exe` into `bin/` first, and `yt-dlp.exe` beside it for the parts
that fetch audio. `bin/README.txt` lists what is looked up, and in what order;
a system `ffmpeg` on `PATH` is fine too.

## Run the converter

```powershell
py -3 -m pip install -r requirements.txt
py -3 app.py                        # http://127.0.0.1:7860
```

`index.html` is the page the Space serves, and it runs off any file server, so
the browser version can be tried without deploying anything:

```powershell
py -3 -m http.server 8000           # from the repository root
```

then open <http://localhost:8000/>.  The page fetches `app.py`, `core.py` and
`webapp/*.py` from the same origin, so it has to be the *repository root* that
is being served - `index.html` and `core.py` sit side by side there.  Nothing is
uploaded: Pyodide and ffmpeg.wasm do the conversion inside the tab.  Both halves
convert identically, because both drive `webapp/converter.py` and neither
changes it.

The converter's page, its engine and the project's own notes are all one story:
[`webapp/README.md`](webapp/README.md) is the engine's documentation - the
formats it writes, the trim and normalise options, and every environment
variable read anywhere in the web half.

A converted file arrives as a normal browser download, and the natural place to
put it is `discs/` next to the player - the player already treats that folder
as part of your library, so it shows up in the playlist picker on its own.

Pick **Minecraft music player** instead and the download is not a bare audio
file but an addon (`.mcaddon`): a resource pack carrying the song, and a small
behavior pack that draws the player.  Open it to add both packs to Minecraft,
turn them on for a world, and run `/yt2disc:music` to pick a song.  The way in
is a custom command, which - like the form API it opens - is stable, so the
world needs no experiment switched on and works offline; `/scriptevent
yt2disc:menu` opens the same menu on a game too old for custom commands.

The addon needs Minecraft **1.21 or newer** - the `min_engine_version` in both
manifests is `1.21.0` - because `/yt2disc:music` is a *custom command*, a stable
API from that release on. On anything older, or if the command is refused, the
same menu still opens through the script-event fallback, which needs no command
list at all:

```
/scriptevent yt2disc:menu
```

Both entry points run the same callback, and neither asks for cheats or an
experiment, so an ordinary world is enough.

## Deploy the converter

Hugging Face Spaces is the completely free home for the converter: a Space needs
no billing account and no card. The YAML block at the very top of this file *is*
the Space's configuration, which is why it is there: `sdk: static` tells Hugging
Face to serve this repository as plain files, and `app_file: index.html` says
which page to open. GitHub renders that block as a small table and otherwise
ignores it; Hugging Face reads it on every build.

Static is the one kind of Space that asks nothing of the host, because it runs
nothing: there is no Python process here and no ffmpeg for one to start. So the
page brings both. `index.html` loads Gradio Lite, which runs Gradio itself in the
browser on [Pyodide](https://pyodide.org), and
[`webapp/browser_ffmpeg.py`](webapp/browser_ffmpeg.py) runs the conversion on
[ffmpeg.wasm](https://ffmpegwasm.netlify.app) - the same FFmpeg compiled to
WebAssembly, libvorbis and libopus included. The page fetches `app.py`, `core.py`
and `webapp/*.py` off the Space as it loads, so those files are the whole
deployment: no build step, no image, and neither `packages.txt` nor
`requirements.txt` involved.

Create the Space, make it a second remote of this repository, and push. Nothing
has to be installed locally and nothing is built anywhere.

```powershell
git remote add space https://huggingface.co/spaces/<user>/yt2disc-converter
git push --force space HEAD:main
```

The push replaces the `README.md` Hugging Face generated for the new Space with
this repository's, so the configuration above is the one that takes effect.

Open

```
https://<user>-yt2disc-converter.hf.space/
```

once. The first visit is the slow one - Gradio's Pyodide runtime and ffmpeg.wasm
add up to about thirty megabytes - and the browser caches both, so every visit
after that is quick.

What `/healthz` used to answer is now printed on the page itself, and on the
Space it says something new: there is no server-side ffmpeg to find, so the line
under the title says where the work will happen instead.

```
ffmpeg: in this browser
```

**There is no shared key any more, and nothing left for one to protect.** The
token existed because a hosted converter is an ffmpeg job that any stranger can
start on your machine. Here the job runs on the stranger's machine, on the
stranger's own file, and costs the Space nothing but a page load - so
`YT2DISC_WEB_TOKEN` is read by the server halves only (`python app.py` on a box
you are exposing, and the Flask front-end) and the Space needs no secret set at
all. The same reasoning retires `YT2DISC_WEB_KEEP_MINUTES` and the scratch folder
with it: a browser's filesystem dies when the tab does.

Free Spaces sleep after 48 idle hours and wake on the next visit. A Static Space
has nothing to sleep - no process to keep alive, no conversion left running on
the host - so the page is simply there when someone opens it.

### The server front-ends, if you want them

`webapp/app.py` is the Flask server that came first, and it is still here with
its own deployment files: [`render.yaml`](render.yaml) for Render, the
`Dockerfile` for an image, the `Aptfile` for Heroku, `webapp/Procfile` for a
Procfile host. It drives the same engine, so it converts identically. What it
has that neither the Space nor the browser page does is a `/healthz` endpoint a
platform can poll and a `?key=` lock that works without a browser prompt.

`python app.py` is the third way to run this same page: Gradio served by a real
Python process, on a host that provides ffmpeg. That is the one that reads
`YT2DISC_WEB_TOKEN`, keeps its scratch folders, and can be put on the open
internet behind a shared key - the browser page needs none of it.

None of that is needed for the Space above. They are two front-ends over one
engine - `webapp/converter.py`, which neither of them changes:

| | `app.py` (Gradio) | `webapp/app.py` (Flask) |
| --- | --- | --- |
| dependencies | `requirements.txt` | `webapp/requirements.txt` |
| start it with | `python app.py` | `python -m webapp.app` |
| default port | 7860 | 8000 |
| ffmpeg comes from | ffmpeg.wasm in the browser, `packages.txt` on a host | `Dockerfile` / `Aptfile` / the host |
| its own notes | this file | [`webapp/README.md`](webapp/README.md) |

ffmpeg is the only binary involved and it is never a Python package, so somebody
has to provide it: the browser downloads its own copy as ffmpeg.wasm and needs
nothing installed, `packages.txt` beside `app.py` is where a Gradio or Docker
Space's copy comes from, Render's native runtime already ships one on `PATH`, and
the `Dockerfile` and `Aptfile` in this root cover the hosts that need it
installed instead. The table in
[`webapp/README.md`](webapp/README.md#deploying) has the row for each host.

## Package the player

```powershell
py -3 build_exe.py                  # -> dist/yt2disc/
```

The build runs the end-to-end test against the packaged executables unless you
pass `--no-test`.

## Checking it works

```powershell
py -3 webapp/_selftest.py           # the conversion engine
py -3 webapp/_webcheck.py           # the whole web app: upload -> convert -> download
```

Each exits non-zero if anything is wrong, so either works as a smoke test after
a change. `app.py` drives the same engine, so `_selftest.py` covers the half of
the Gradio page that can fail on its own; `_webcheck.py` covers the Flask
front-end instead, because the page does not use it. For the page itself, `py -3 app.py`
and one conversion is the check.

Both are what `.github/workflows/ci.yml` runs on every push - alongside the
end-to-end suite - so a change that breaks either is caught without anyone
remembering to look.

Nothing here runs in a browser, so the browser half has no committed test: the
page is Pyodide and WebAssembly, and neither exists outside one. What it shares
with the tested half is the part that can actually be wrong - `converter.py`,
which both drive unchanged - and `browser_ffmpeg.py` borrows core's own parsing
instead of restating it, so the percentages cannot drift apart. Trying the page
once is the check for the rest: `py -3 -m http.server 8000`, one conversion, and
the ffmpeg line the page prints.
