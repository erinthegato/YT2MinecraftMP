# yt2disc converter (the hosted half)

The other half of yt2disc is the **local add-on**: `player.py`, `gui.py` and
`cli.py` play the music, keep the playlists and quieten Minecraft's own
soundtrack. This folder is the part that can live on the web, and it does
exactly one thing:

> take a file, turn it into another file, hand it back.

There is no player here, no library, no account, and nothing that reads a
Minecraft install. That split is deliberate: converting is the part worth
offloading to a server (it wants ffmpeg, and it takes a while), while playing
is local (it needs your speakers, your files and your game).

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
| Format | Ogg Vorbis (the default - what Minecraft resource packs read), Opus, MP3, AAC/m4a, WAV, FLAC |
| Quality | 96k - 320k; silently ignored by WAV and FLAC, which cannot use it |
| Sample rate | keep the original, or 22050 / 44100 / 48000 Hz |
| Channels | keep, mono, or stereo |
| Trim | start and end, as seconds (`90`) or `mm:ss` (`1:30`) |
| Normalise | EBU R128 loudness, so one track is not far louder than the game it plays over |

Uploads may be audio or video; from a video only the sound is kept. A trim that
points past the end of the file is refused with a readable message rather than
handed back as a broken file.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8000` | port for `python -m webapp.app` |
| `YT2DISC_WEB_HOST` | `127.0.0.1` | bind address; use `0.0.0.0` on a host |
| `YT2DISC_WEB_DATA` | the system temp folder | where uploads and results are written |
| `YT2DISC_WEB_MAX_UPLOAD_MB` | `200` | upload ceiling; above it you get a 413 page |
| `YT2DISC_WEB_SLOTS` | `2` | conversions allowed to run at the same time |
| `YT2DISC_WEB_KEEP_MINUTES` | `120` | how long a finished download stays available |
| `YT2DISC_FFMPEG` | unset | path to the ffmpeg binary |
| `YT2DISC_WEB_TOKEN` | unset | shared key every visitor must give; unset means wide open |

The player's own variables (`YT2DISC_ROOT`, `YT2DISC_MINECRAFT`, ...) are *not*
used here: this half never reads a library or a Minecraft folder.

## Deploying

`render.yaml` in the repository root is a Render Blueprint: connect the
repository, and it installs `webapp/requirements.txt`, starts
`python -m webapp.app`, points the health check at `/healthz`, and generates a
`YT2DISC_WEB_TOKEN` so the deployed converter is not open to everyone who finds
the URL. Read the key from the service's Environment page and visit
`https://<service>.onrender.com/?key=<key>` once.

The generated key is base64, so it can contain `+`, `/` and `=`. Paste it
exactly as the dashboard shows it: `?key=a+b` and `?key=a%2Bb` are both
understood, because a bare `+` in a query string otherwise means a space.

`webapp/Procfile` holds the line a Procfile host needs:

```
web: YT2DISC_WEB_HOST=0.0.0.0 python -m webapp.app
```

On Render/Fly/Heroku, point the service at this repository and make sure ffmpeg
exists in the image - it is not a Python package, so the host has to provide it.
Pick whichever line matches the host; the app itself does not care where ffmpeg
came from, only that `ffmpeg` can be run:

| Host | What to do |
| --- | --- |
| Render (native runtime) | nothing - the Python runtime already ships `ffmpeg` on `PATH`; `/healthz` proves it |
| Heroku | commit the `Aptfile` in the repository root and add the buildpack once: `heroku buildpacks:add --index 1 heroku-community/apt` |
| Fly.io | commit `fly.toml` in the repository root and run `fly deploy`: it builds the `Dockerfile` (ffmpeg included) and keeps one Machine permanently awake, so a conversion on a background thread is never interrupted |
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
| `converter.py` | the ffmpeg work: options, the command line, progress parsing, file naming. No web framework, no globals |
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
after a change.

