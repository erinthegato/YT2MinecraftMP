---
title: yt2disc converter
emoji: 🎵
colorFrom: green
colorTo: blue
sdk: gradio
app_file: app.py
pinned: false
---

# yt2disc

Two halves of one idea, split on purpose:

| Half | Where it runs | What it does |
| --- | --- | --- |
| the local add-on | your PC - `main.py`, `gui.py`, `cli.py`, `player.py` | plays the music you already own: your own audio files, and the soundtrack that ships inside Minecraft for Windows. It builds playlists and can mute Minecraft's own background music while it plays, so a custom song is never fighting the game |
| `app.py` | anywhere, including a server | the page a visitor uses: a file goes in, a file comes out. Gradio draws it - the form, the progress bar, the download link |
| `webapp/` | behind that page | the conversion engine: what to run ffmpeg with, how far along it is, what to call the result |

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

The converter's page, its engine and the project's own notes are all one story:
[`webapp/README.md`](webapp/README.md) is the engine's documentation - the
formats it writes, the trim and normalise options, and every environment
variable read anywhere in the web half.

A converted file arrives as a normal browser download, and the natural place to
put it is `discs/` next to the player - the player already treats that folder
as part of your library, so it shows up in the playlist picker on its own.

## Deploy the converter

Hugging Face Spaces is the completely free home for the converter: a Space needs
no billing account and no card, and its free hardware - 2 vCPU and 16 GB of RAM
- is far more than a conversion needs. The YAML block at the very top of this
file *is* the Space's configuration, which is why it is there: `sdk: gradio`
tells Hugging Face to run this repository as a Gradio app, and `app_file: app.py`
says which file starts it. Two files beside `app.py` finish the job:
`requirements.txt` is what the app imports (Gradio itself) and `packages.txt` is
where ffmpeg comes from, because ffmpeg is not a Python package and the page
cannot convert anything without it. GitHub renders that block as a small table
and otherwise ignores it; Hugging Face reads it on every build.

Create a Space with the **Gradio** SDK, then make it a second remote of this
repository and push. Nothing has to be installed locally: the Space builds its
own environment from those two files, so there is no image to build here and no
Docker involved at all.

```powershell
git remote add space https://huggingface.co/spaces/<user>/yt2disc-converter
git push --force space HEAD:main
```

The push replaces the `README.md` Hugging Face generated for the new Space with
this repository's, so the configuration above is the one that takes effect.

Set the shared key as a Space **secret** (*Settings -> Variables and secrets*)
rather than in a file, then open

```
https://<user>-yt2disc-converter.hf.space/
```

once per browser, and the browser remembers the answer after that.

What `/healthz` used to answer is now printed on the page itself: the line under
the title names the ffmpeg that was found, so an image that lost ffmpeg says so
in plain sight instead of failing quietly for every visitor.

```
ffmpeg: /usr/bin/ffmpeg
```

Free Spaces sleep after 48 idle hours and wake on the next visit, and a
conversion is never what puts one to sleep: the page keeps its connection open
while ffmpeg runs, and that connection is traffic.

The token stays optional, exactly as before. A converter on the open internet is
an ffmpeg job that any stranger can start, so the page asks for a shared key
whenever `YT2DISC_WEB_TOKEN` is set - and stays wide open when it is not, which
keeps a run on your own PC behaving exactly as it always has. Gradio's own lock
is HTTP basic auth, so the browser asks once per visit and nothing is put in the
address bar: the username is `yt2disc` and the password is the token, or write
the token as `user:pass` to choose both halves yourself. That is why the old
`?key=` handling is gone - there is no URL left to carry it.

### The other front-end, if you want it

`webapp/app.py` is the Flask server that came first, and it is still here with
its own deployment files: [`render.yaml`](render.yaml) for Render, the
`Dockerfile` for an image, the `Aptfile` for Heroku, `webapp/Procfile` for a
Procfile host. It drives the same engine, so it converts identically. What it
has that the Gradio page does not is a `/healthz` endpoint a platform can poll
and a `?key=` lock that works without a browser prompt.

None of that is needed for the Space above. They are two front-ends over one
engine - `webapp/converter.py`, which neither of them changes:

| | `app.py` (Gradio) | `webapp/app.py` (Flask) |
| --- | --- | --- |
| dependencies | `requirements.txt` | `webapp/requirements.txt` |
| start it with | `python app.py` | `python -m webapp.app` |
| default port | 7860 | 8000 |
| ffmpeg comes from | `packages.txt` | `Dockerfile` / `Aptfile` / the host |
| its own notes | this file | [`webapp/README.md`](webapp/README.md) |

ffmpeg is the only binary involved and it is not a Python package, so the host
has to provide it: `packages.txt` beside `app.py` is where the Space's copy comes
from, Render's native runtime already ships one on `PATH`, and the `Dockerfile`
and `Aptfile` in this root cover the hosts that need it installed instead. The
table in [`webapp/README.md`](webapp/README.md#deploying) has the row for each
host.

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
front-end instead, because the page does not use it. For the page itself,
`py -3 app.py` and one conversion is the check.
