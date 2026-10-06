"""_webcheck.py - drive the real web app: upload, convert, download.

    python webapp/_webcheck.py

Two passes:

* the Flask test client runs the whole journey in-process - post a generated
  file to ``/convert``, poll the job, fetch ``/download`` and check the bytes
  really are Ogg Vorbis - plus the refusals that must never answer 200;
* the app is then started for real with ``python -m webapp.app`` on a spare
  port and ``/`` and ``/healthz`` are fetched over HTTP, because a test client
  never proves that a port, a bind address and a WSGI server line up.

The exit code is the number of failures.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _entry in (HERE, ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import core  # noqa: E402
from app import create_app  # noqa: E402
from jobs import JobRegistry  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}: got {got!r}, wanted {want!r}")
    print(f"  FAIL {label}: got {got!r}, wanted {want!r}")


def tone_bytes(ffmpeg, seconds: int = 2) -> bytes:
    """A small, real WAV to upload."""
    with tempfile.TemporaryDirectory() as workdir:
        path = Path(workdir) / "tone.wav"
        subprocess.run(
            [
                str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
                "-ac", "2", "-ar", "44100", str(path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return path.read_bytes()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def get(url, timeout=10):
    """``(status, body)``; an error status is data here, not an exception."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def check_pages(client) -> None:
    """The pages a visitor sees, including the ones that should say no."""
    page = client.get("/")
    check("GET / is 200", page.status_code, 200)
    check("the form posts to /convert", b'action="/convert"' in page.data, True)
    check("the form offers a convert button", b"Convert it" in page.data, True)
    check("the form asks for a file", b'name="media"' in page.data, True)

    health = client.get("/healthz")
    check("GET /healthz is 200", health.status_code, 200)
    check("healthz found ffmpeg", (health.get_json() or {}).get("ok"), True)

    check("an unknown job is a 404", client.get("/job/nope").status_code, 404)
    check("an unknown download is a 404", client.get("/download/nope").status_code, 404)


def check_conversion(client, scratch, ffmpeg) -> None:
    """Upload, wait, download - and inspect what came back."""
    response = client.post(
        "/convert",
        data={
            "media": (io.BytesIO(tone_bytes(ffmpeg)), "My Song.wav"),
            "format": "ogg",
            "bitrate": "192k",
            "sample_rate": "44100",
            "channels": "mono",
            "start": "0.5",
            "end": "1.5",
            "normalize": "on",
        },
        content_type="multipart/form-data",
    )
    check("POST /convert redirects", response.status_code, 303)
    location = response.headers.get("Location", "")
    check("it redirects to /job/<id>", location.startswith("/job/"), True)
    job_id = location.rsplit("/", 1)[-1]

    data: dict = {}
    deadline = time.time() + 90
    while time.time() < deadline:
        data = client.get(f"/api/jobs/{job_id}").get_json() or {}
        if data.get("status") in ("done", "error"):
            break
        time.sleep(0.2)

    if data.get("status") == "error":
        FAILURES.append(f"the conversion failed: {data.get('error')}")
    check("the job finished", data.get("status"), "done")
    check("the trim is in the file name", data.get("output_name"),
          "my_song-from0.5s-to1.5s.ogg")
    check("it reported a length", data.get("duration_text") not in (None, "--:--"), True)
    check("it reported a size", bool(data.get("size_text")), True)
    check("the API offered a download", data.get("download_url"),
          f"/download/{job_id}")

    page = client.get(f"/job/{job_id}")
    check("the job page renders", page.status_code, 200)
    check("the job page links the file", b"/download/" in page.data, True)

    grabbed = client.get(f"/download/{job_id}")
    check("GET /download is 200", grabbed.status_code, 200)
    check("it is sent as an attachment",
          "attachment" in grabbed.headers.get("Content-Disposition", ""), True)
    check("the bytes are Ogg Vorbis", grabbed.data[:4], b"OggS")
    check("the download has real audio in it", len(grabbed.data) > 1000, True)

    check("the uploaded original was cleaned up",
          list(Path(scratch).glob("*/input.*")), [])


def check_refusals(client) -> None:
    """Everything that should come back as a readable 400, not a 500."""
    for name in ("notes.txt", "song.mid"):
        response = client.post(
            "/convert",
            data={"media": (io.BytesIO(b"not audio at all"), name)},
            content_type="multipart/form-data",
        )
        check(f"a {name} upload is refused", response.status_code, 400)
        check(f"the {name} refusal explains itself",
              b"not a file we can read" in response.data, True)

    empty = client.post("/convert", data={}, content_type="multipart/form-data")
    check("an empty form is refused", empty.status_code, 400)
    check("the empty form says what to do", b"choose a file" in empty.data, True)

    backwards = client.post(
        "/convert",
        data={
            "media": (io.BytesIO(b"x" * 64), "song.wav"),
            "format": "mp3",
            "start": "10",
            "end": "5",
        },
        content_type="multipart/form-data",
    )
    check("a backwards trim is refused", backwards.status_code, 400)
    check("the backwards trim is explained",
          b"has to come after the start" in backwards.data, True)

    unknown_format = client.post(
        "/convert",
        data={"media": (io.BytesIO(b"x" * 64), "song.wav"), "format": "aiff"},
        content_type="multipart/form-data",
    )
    check("an unknown format is refused", unknown_format.status_code, 400)


def check_real_server() -> None:
    """Start the app the way a host would, and talk to it over HTTP."""
    port = free_port()
    env = dict(os.environ)
    env.update(
        {
            "PORT": str(port),
            "YT2DISC_WEB_HOST": "127.0.0.1",
            "YT2DISC_WEB_DATA": tempfile.mkdtemp(prefix="yt2disc-live-"),
            "PYTHONIOENCODING": "utf-8",
            "YT2DISC_WEB_TOKEN": "",  # the harness always runs unlocked
        }
    )
    base = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [sys.executable, "-m", "webapp.app"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        status = None
        body = b""
        deadline = time.time() + 30
        while time.time() < deadline:
            if process.poll() is not None:
                break  # it died; no point waiting for the rest of the timeout
            try:
                status, body = get(f"{base}/healthz", timeout=2)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)

        if status is None:
            FAILURES.append("the app never answered on a real port")
            print("  FAIL the app never answered /healthz")
            output = process.stdout.read() if process.stdout else ""
            print("  the server said:", (output or "(nothing)")[-900:])
            return

        check("/healthz over HTTP is 200", status, 200)
        check("/healthz reports ffmpeg over HTTP",
              (json.loads(body) or {}).get("ok"), True)

        home_status, home = get(f"{base}/")
        check("GET / over HTTP is 200", home_status, 200)
        check("the whole page is served over HTTP", b"Convert it" in home, True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            process.kill()


def check_access_key(scratch) -> None:
    """A key, when one is set, locks the pages but never the health check.

    Built as an app of its own so the unlocked one stays untouched: the key is
    read when ``create_app`` runs, not when this module is imported.
    """
    os.environ["YT2DISC_WEB_TOKEN"] = "a-test-key"
    try:
        locked = create_app(
            JobRegistry(root=Path(scratch), slots=1, keep_seconds=600)
        )
    finally:
        os.environ.pop("YT2DISC_WEB_TOKEN", None)

    client = locked.test_client()
    check("a locked /healthz still answers 200",
          client.get("/healthz").status_code, 200)
    check("a locked / asks for the key", client.get("/").status_code, 401)
    check("the wrong key is refused", client.get("/?key=no").status_code, 401)
    handed_over = client.get("/?key=a-test-key")
    check("the right key is accepted", handed_over.status_code, 303)
    check("and taken back out of the address bar",
          handed_over.headers.get("Location"), "/")
    check("the cookie it left is enough after that",
          client.get("/").status_code, 200)


def main() -> int:
    print("== the app, in process ==")
    if core.find_binary("ffmpeg") is None:
        FAILURES.append("ffmpeg not found - the conversion checks cannot run")
        print("  FAIL ffmpeg not found")
        return report()

    scratch = tempfile.mkdtemp(prefix="yt2disc-webcheck-")
    app = create_app(JobRegistry(root=Path(scratch), slots=1, keep_seconds=600))
    client = app.test_client()

    check_pages(client)
    check_conversion(client, scratch, core.find_binary("ffmpeg"))
    check_refusals(client)
    check_access_key(scratch)

    print("== the app, on a real port ==")
    check_real_server()
    return report()


def report() -> int:
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for line in FAILURES:
            print(f"  - {line}")
        return len(FAILURES)
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


