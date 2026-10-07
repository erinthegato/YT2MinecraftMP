# yt2minecraftdisc converter (the hosted half)

The other half of yt2disc is the **local add-on**: `player.py`, `gui.py` and
`cli.py` play the music, keep the playlists and quieten Minecraft's own
soundtrack. This folder holds the engine of the part that lives on the web, and
it does exactly one thing:

> take a file, turn it into another file, hand it back.

There is no player here, no library, no account, and nothing that reads a
Minecraft install. That split is deliberate: converting is the part worth
offloading to a server (it wants ffmpeg, and it takes a while), while playing
is local (it needs your speakers, your files and your game).

`converter.py` is that engine. Three front-ends sit on top of it, and none of
them changes it:

| Front-end | Start it with | Where it belongs |
| --- | --- | --- |
| [`index.html`](../index.html) in the repository root, running Gradio Lite | open it from any file server | the recommended deployment: a free Hugging Face **Static** Space, whose `README.md` block says `sdk: static`. No process and no ffmpeg to install - Pyodide and ffmpeg.wasm do the work in the visitor's own tab, through `browser_ffmpeg.py` |
| [`app.py`](../app.py) in the repository root, built with Gradio | `py -3 app.py` | the same page served by a real Python process over a host-supplied ffmpeg |
| [`app.py`](app.py) in this folder, built with Flask | `py -3 -m webapp.app` | the phone helper below, Render, a container, or a box you manage yourself |

A converted file arrives as a normal browser download, and the natural place to
put it is `discs/` next to the player - the player already treats that folder
as part of your library, so it shows up in the playlist picker on its own.

## Run it locally

```powershell
py -3 -m pip install -r webapp/requirements.txt
py -3 -m webapp.app                       # http://127.0.0.1:8000
```

ffmpeg is the only binary it needs, and it is looked up in this order:

1. `YT2DISC_FFMPEG` - an explicit path always wins
2. `ffmpeg` on `PATH` **on Linux/macOS** - what a package manager, a container
   image or the host's own runtime provides
3. `bin/ffmpeg` in this project (already there if you have run the player)
4. `ffmpeg` on `PATH` on Windows, where `bin\ffmpeg.exe` is tried before it so
   the portable build stays self-contained

A file that exists but cannot be run is skipped instead of used: yt2disc adds a
missing execute bit itself when it can, so an ffmpeg copied in without
`chmod +x` still works and a broken leftover in `bin/` can never hide the
system copy.

The banner printed at start-up says which one it chose, and `/healthz` answers
the same question as JSON.

## Reach it from your phone

`python -m webapp.app` listens on `127.0.0.1`, which only this PC can open. The
helper beside it does the opposite, and is what the `yt2disc-web-public.cmd`
launcher in the repository root runs:

```powershell
py -3 -m webapp._host                        # this network: Wi-Fi or a hotspot
py -3 -m webapp._host --tunnel               # and a public https URL as well
py -3 -m webapp._host --fetch-cloudflared    # get cloudflared into bin/ first
```

It binds `0.0.0.0`, opens the Windows Firewall for the port, and prints every
address the phone can use. Where the phone shares your network - the same Wi-Fi,
or a hotspot the PC is joined to - that is the whole story: type the address,
upload, convert, download. The firewall rule needs an administrator once, and if
the console is not elevated the exact `netsh` line to run is printed instead.

`--tunnel` covers the other case: the phone is somewhere else, on cellular, not
on your network at all. It uses [cloudflared](https://github.com/cloudflare/cloudflared),
which `--fetch-cloudflared` drops into `bin/` next to ffmpeg. A *quick tunnel*
needs no Cloudflare account and no domain: cloudflared hands back a throwaway
`https://something.trycloudflare.com` address, a fresh one each run.

Because that address is on the open internet, `--tunnel` also generates a
`YT2DISC_WEB_TOKEN` for the run and prints the URL with `?key=` already on it.
The key is asked for once and then kept in a cookie, so the address bar does not
hold on to it. `--key none` leaves the converter open, `--key mine` uses a key
of your own, and an exported `YT2DISC_WEB_TOKEN` wins over both.

The tunnel dials `127.0.0.1`, so it works even when the firewall rule was never
added and the LAN address is still unreachable.

## What a visitor can choose

| Choice | Notes |
| --- | --- |
| Format | Ogg Vorbis (the default - what Minecraft resource packs read), Opus, MP3, AAC/m4a, WAV, FLAC, or the in-game addon below |
| Quality | 96k - 320k; silently ignored by WAV and FLAC, which cannot use it |
| Minecraft music player | not a codec: the audio is written as Ogg and then wrapped, with a small behavior pack, into an `.mcaddon`. Add it to Minecraft, turn the resource and behavior packs on for a world, and `/yt2disc:music` opens a menu of the songs |
| Sample rate | keep the original, or 22050 / 44100 / 48000 Hz |
| Channels | keep, mono, or stereo |
| Trim | start and end, as seconds (`90`) or `mm:ss` (`1:30`) |
| Normalise | EBU R128 loudness, so one track is not far louder than the game it plays over |

Uploads may be audio or video; from a video only the sound is kept. A trim that
points past the end of the file is refused with a readable message rather than
handed back as a broken file.

The addon needs Minecraft **1.21 or newer** (both manifests say
`min_engine_version` `1.21.0`): `/yt2disc:music` is a custom command, a stable
API from that release on. On an older game - or if the command is refused - the
same menu opens with `/scriptevent yt2disc:menu`, which needs no command list.
Both routes run the same callback, so neither needs cheats or an experiment.

## Environment variables

The Gradio page at the repository root reads the same names, plus the two that
Gradio itself uses for the address: `GRADIO_SERVER_PORT` (default `7860`) and
`GRADIO_SERVER_NAME`. `YT2DISC_WEB_SLOTS` is the one exception - Gradio's own
queue allows one conversion at a time, so the page ignores it.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8000` (Gradio: `7860`) | port to listen on |
| `GRADIO_SERVER_PORT` / `GRADIO_SERVER_NAME` | unset | the same two, under the names a Space sets for us |
| `YT2DISC_WEB_HOST` | `127.0.0.1` | bind address; use `0.0.0.0` on a host |
| `YT2DISC_WEB_DATA` | the system temp folder | where uploads and results are written |
| `YT2DISC_WEB_MAX_UPLOAD_MB` | `200` | upload ceiling; above it the file is refused with a reason |
| `YT2DISC_WEB_SLOTS` | `2` | conversions allowed to run at the same time (Flask front-end only) |
| `YT2DISC_WEB_JOB_TIMEOUT_MINUTES` | `30` | stop a conversion that runs longer than this; `0` means no limit |
| `YT2DISC_WEB_KEEP_MINUTES` | `120` | how long a finished download stays available |
| `YT2DISC_FFMPEG` | unset | path to the ffmpeg binary |
| `YT2DISC_WEB_TOKEN` | unset | shared key every visitor must give; unset means wide open |

The player's own variables (`YT2DISC_ROOT`, `YT2DISC_MINECRAFT`, ...) are *not*
used here: this half never reads a library or a Minecraft folder.

## Deploying

The free host this repository is set up for is
[Hugging Face Spaces](https://huggingface.co/docs/hub/spaces-sdks-static), and it
serves the **Static** page at the repository root rather than the Flask server in
this folder. Create a Space with the **Static** SDK, add it as a second remote of
this repository, and push:

```powershell
git remote add space https://huggingface.co/spaces/<user>/yt2disc-converter
git push --force space HEAD:main
```

What Hugging Face reads is the YAML block at the top of the repository's
`README.md`: `sdk: static` with `app_file: index.html` hands the repository to the
browser as plain files, and `index.html` is the page it opens. That page loads
Gradio Lite - Gradio itself, running in the browser on Pyodide - and the same page
fetches `app.py`, `core.py` and everything under `webapp/` off the Space as it
starts, which is why the files in this folder ship too. Nothing is built and
nothing runs on the host: no Python process, no image, no server-side ffmpeg.
Conversion happens in the visitor's browser through `webapp/browser_ffmpeg.py` and
ffmpeg.wasm, so neither the root `requirements.txt` nor `packages.txt` is
installed - the first visit is the slow one, pulling about thirty megabytes of
Pyodide and ffmpeg.wasm into the browser cache.

There is no shared key on this deployment, because there is nothing on the host
for one to protect: the job runs on the visitor's machine against the visitor's own
file. `YT2DISC_WEB_TOKEN` is read by the server halves only - `python app.py` on a
box you are exposing, and the Flask front-end below - so the Space needs no secret
set at all and the address to open is plain
`https://<user>-yt2disc-converter.hf.space/`.

A Static Space has no process to sleep and no conversion left running on the host,
so the page is simply there when someone opens it.

### Deploying *this* Flask server instead

Everything below is about the Flask front-end in this folder. It drives the same
engine, so it converts identically, and it brings the two things the Static page
does not: a `/healthz` endpoint a platform can poll, and a `?key=` lock that
works without a browser prompt.

`render.yaml` in the repository root is the same deployment on Render, whose
free instance type is 0.1 CPU and 512 MB and sleeps after about 15 idle minutes.
Connect the repository and the Blueprint installs `webapp/requirements.txt`,
starts `python -m webapp.app`, points the health check at `/healthz`, and
generates the same `YT2DISC_WEB_TOKEN`; read the key from the service's
Environment page and visit `https://<service>.onrender.com/?key=<key>` once.

A generated key is base64, so it can contain `+`, `/` and `=`. Paste it exactly
as the dashboard shows it: `?key=a+b` and `?key=a%2Bb` are both understood,
because a bare `+` in a query string otherwise means a space.

`webapp/Procfile` holds the line a Procfile host needs:

```
web: YT2DISC_WEB_HOST=0.0.0.0 python -m webapp.app
```

Whichever host you pick, make sure `ffmpeg` exists in the image - it is not a
Python package, so the host has to provide it. The app itself does not care
where ffmpeg came from, only that `ffmpeg` can be run. (Nothing below applies to
the repository root's `index.html`: a Static Space installs nothing, and that
page converts with ffmpeg.wasm in the browser. These rows are for the two server
front-ends and for this folder's Flask app.)

| Host | What to do |
| --- | --- |
| Hugging Face Spaces (Static) | nothing to install at all: the block at the top of `README.md` supplies `sdk: static` and `app_file: index.html`, and the browser downloads ffmpeg.wasm itself |
| Hugging Face Spaces (Gradio or Docker) | the root `packages.txt` installs ffmpeg; a Gradio Space reads the root `requirements.txt` as well |
| Render (native runtime) | nothing for ffmpeg - the Python runtime already ships it on `PATH`; `/healthz` proves it |
| Heroku | commit the `Aptfile` in the repository root and add the buildpack once: `heroku buildpacks:add --index 1 heroku-community/apt` |
| Kubernetes, any `docker run` | build the `Dockerfile` in the repository root: it installs ffmpeg, installs `webapp/requirements.txt`, and starts `python -m webapp.app` |
| a box you manage yourself | `sudo apt install ffmpeg`, drop a static build into `bin/`, or set `YT2DISC_FFMPEG` |

With Docker:

```bash
docker build -t yt2disc-converter .
docker run --rm -p 8000:8000 yt2disc-converter
```

Any WSGI server works too:

```bash
pip install gunicorn
gunicorn --bind 0.0.0.0:${PORT:-8000} --threads 4 'webapp.app:create_app()'
```

Point the host's health check at `/healthz`, not `/`: it answers `503` when
ffmpeg is missing, so a broken deploy is obvious instead of quietly failing
every visitor.

## How the pieces fit

| File | Job |
| --- | --- |
| [`../index.html`](../index.html) | the Static Space's page and the whole of its server: loads Gradio Lite, fetches the project's Python off the Space, and runs `../app.py` in the browser |
| [`../app.py`](../app.py) | the Gradio page: the same conversion drawn with `gr.Blocks` instead of templates. It calls `converter.py` directly and lets Gradio's queue and progress bar do what `jobs.py` does here - in a browser and on a server, unchanged |
| `browser_ffmpeg.py` | the browser's ffmpeg, which is not a program: it drives ffmpeg.wasm and rebinds `core.run_ffmpeg`, `core.probe_duration` and `core.find_binary`, so `converter.py` needs no browser branch at all |
| `converter.py` | the ffmpeg work: options, the command line, progress parsing, file naming. No web framework, no globals, and no idea whether it is in a browser |
| `packs.py` | the in-game addon: two manifests, `sound_definitions.json`, the menu script, and a pack icon drawn with `zlib` alone. Standard library only, because it also has to run on Pyodide |
| `jobs.py` | runs conversions on worker threads, tracks progress, deletes scratch files |
| `app.py` | the thin Flask layer: the pages, the polling endpoint, the download |
| `_host.py` | shows this app to a phone: widen the bind, open the firewall, print the address, optional tunnel |
| `templates/`, `static/` | the form, the progress page, one stylesheet, one script |

Conversions happen **outside** the request: `/convert` saves the upload, starts
a job and redirects to `/job/<id>`, which polls `/api/jobs/<id>`. A long encode
therefore cannot time out a request, and the page still works (minus the live
progress bar) with JavaScript off. The uploaded original is deleted the moment
the conversion succeeds, and both the output and the job are dropped once the
job ages out.

## Checking it works

```powershell
py -3 webapp/_selftest.py   # the engine: real ffmpeg conversion, trim, progress
py -3 webapp/_webcheck.py   # the whole app: upload -> convert -> download
```

Each exits non-zero if anything is wrong, so either can be used as a smoke test
after a change. Neither covers the browser: `browser_ffmpeg.py` needs a browser
to run in at all, and the page around it does too. What they do cover is the
engine it borrows, unchanged. For the rest, `py -3 -m http.server 8000` from the
repository root, one conversion, and the ffmpeg line the page prints is the
check.

