"""app.py - the hosted half of yt2disc: upload, convert, download.

This is a *converter and nothing else*.  It has no player, keeps no library and
knows nothing about Minecraft: one file in, one file out, in the format you
ask for.  Playing the result is the local add-on's job.

    python webapp/app.py                          # http://127.0.0.1:8000
    flask --app webapp.app run                    # Flask's development server
    waitress-serve --call webapp.app:create_app   # what a host would run

Environment variables, all optional:

    PORT                       port for ``python webapp/app.py`` (default 8000)
    YT2DISC_WEB_HOST           address to listen on (default 127.0.0.1)
    YT2DISC_WEB_DATA           scratch folder (default: the system temp dir)
    YT2DISC_WEB_MAX_UPLOAD_MB  upload cap (default 200)
    YT2DISC_WEB_SLOTS          conversions at once (default 2)
    YT2DISC_WEB_KEEP_MINUTES   how long a finished download stays (default 120)
    YT2DISC_FFMPEG             path to ffmpeg, otherwise PATH then bin/
    YT2DISC_WEB_TOKEN          shared key visitors must give (default: off)
"""

from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # `import converter` / `import jobs` next to us
    sys.path.insert(0, str(HERE))

from flask import (  # noqa: E402
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge  # noqa: E402

import converter  # noqa: E402
import core  # noqa: E402
from jobs import JobRegistry  # noqa: E402

APP_NAME = "yt2minecraftdisc converter"
MEGABYTE = 1024 * 1024


def int_env(name: str, default: int, low: int = 1, high: int = 1_000_000) -> int:
    """An integer environment variable, clamped, falling back when unreadable."""
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def access_key() -> str:
    """The shared key a deployment asks for; empty - the default - means open.

    A converter on the open internet is an ffmpeg job that anyone who finds the
    URL can start, so ``YT2DISC_WEB_TOKEN`` puts a lock on the door.  Unset, it
    changes nothing at all: the desktop shortcut behaves exactly as before.
    """
    return (os.environ.get("YT2DISC_WEB_TOKEN") or "").strip()


def make_registry() -> JobRegistry:
    """The job store, sized from the environment."""
    return JobRegistry(
        root=os.environ.get("YT2DISC_WEB_DATA") or None,
        slots=int_env("YT2DISC_WEB_SLOTS", 2, 1, 16),
        keep_seconds=int_env("YT2DISC_WEB_KEEP_MINUTES", 120, 1, 100000) * 60.0,
    )


def form_defaults() -> dict:
    """What the form shows before anything is typed."""
    return {
        "format": converter.DEFAULT_FORMAT,
        "bitrate": converter.DEFAULT_BITRATE,
        "sample_rate": 0,
        "channels": "auto",
        "start": "",
        "end": "",
        "normalize": False,
    }


def create_app(registry: JobRegistry | None = None) -> Flask:
    """Build the app.  A host (or a test) can pass in its own registry."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = (
        int_env("YT2DISC_WEB_MAX_UPLOAD_MB", 200, 1, 4096) * MEGABYTE
    )
    jobs = registry if registry is not None else make_registry()
    app.extensions["yt2disc_jobs"] = jobs

    # ---- the door, when there is one -------------------------------------
    key = access_key()
    app.config["YT2DISC_WEB_KEY"] = key

    if key:
        cookie = "yt2disc_key"

        def carries_key(candidate: str | None) -> bool:
            """Constant time, because a short key is still a secret."""
            if not candidate:
                return False
            return hmac.compare_digest(
                candidate.encode("utf-8"), key.encode("utf-8")
            )

        def raw_key_argument() -> str | None:
            """``key`` from the query string, still exactly as it was sent.

            ``request.args`` has already turned every ``+`` into a space by the
            time it can be read, and a generated key is base64 - which carries
            a ``+`` about as often as not.  Reading the raw string as well is
            what makes the literal ``?key=a+b`` work exactly like the
            percent-encoded ``?key=a%2Bb``, instead of quietly answering 401 to
            half the keys a host could have generated.
            """
            for field in request.query_string.decode("latin-1").split("&"):
                name, _, value = field.partition("=")
                if name == "key":
                    return value
            return None

        @app.before_request
        def require_key():
            """Everything but the health check asks for the key - once.

            The check answers before any upload is read or any conversion is
            started, so a locked converter does no work for a stranger.
            """
            if request.path == "/healthz" or request.path.startswith("/static/"):
                return None
            if carries_key(request.headers.get("X-YT2DISC-Key")):
                return None
            if carries_key(request.cookies.get(cookie)):
                return None
            if carries_key(request.args.get("key")) or carries_key(
                raw_key_argument()
            ):
                # Remember it, then take it back out of the address bar: a key
                # in a URL survives in history, screenshots and the next
                # person's typing.  A GET is redirected; a POST carries on.
                if request.method in ("GET", "HEAD") and request.endpoint:
                    wanted = dict(request.args.to_dict(flat=True))
                    wanted.pop("key", None)
                    wanted.update(request.view_args or {})
                    response = redirect(
                        url_for(request.endpoint, **wanted), code=303
                    )
                    response.set_cookie(
                        cookie,
                        key,
                        max_age=60 * 60 * 24 * 30,
                        httponly=True,
                        samesite="Lax",
                        secure=request.is_secure,
                    )
                    return response
                return None
            return render_template("key.html", app_name=APP_NAME), 401

    def index_context(values=None, error=None) -> dict:
        """Everything the form needs, on a first visit or after a complaint."""
        return {
            "app_name": APP_NAME,
            "formats": converter.format_choices(),
            "bitrates": converter.BITRATES,
            "sample_rates": list(converter.SAMPLE_RATES),
            "channels": converter.CHANNEL_CHOICES,
            "inputs": converter.SUPPORTED_INPUTS,
            "ffmpeg": core.find_binary("ffmpeg"),
            "max_upload_mb": app.config["MAX_CONTENT_LENGTH"] // MEGABYTE,
            "values": values or form_defaults(),
            "error": error,
        }

    # ---- pages -----------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html", **index_context())

    @app.post("/convert")
    def start_conversion():
        """Take the upload, start a job, and send the browser to its page."""
        values = form_defaults()
        for key in values:
            if key in request.form:
                values[key] = request.form.get(key)
        values["normalize"] = "normalize" in request.form  # unchecked == absent

        upload = request.files.get("media")
        try:
            if upload is None or not (upload.filename or "").strip():
                raise core.InputError("choose a file to convert first")
            options = converter.normalize_options(request.form)
            job = jobs.submit(upload.filename, upload.stream, options)
        except core.YT2DiscError as exc:
            return render_template("index.html", **index_context(values, str(exc))), 400
        # 303, not the default 302: it tells the browser to *GET* the job page
        # instead of repeating this POST there, which the job URL would refuse.
        return redirect(url_for("job_page", job_id=job.id), code=303)

    @app.get("/job/<job_id>")
    def job_page(job_id):
        job = jobs.get(job_id)
        if job is None:
            return render_template("missing.html", app_name=APP_NAME, job_id=job_id), 404
        return render_template(
            "job.html",
            app_name=APP_NAME,
            job=job.as_dict(),
            download_url=(
                url_for("download", job_id=job.id) if job.status == "done" else None
            ),
        )

    # ---- the bits the page talks to --------------------------------------
    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        """What the progress bar polls."""
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "that job is gone, or never existed"}), 404
        data = job.as_dict()
        data["download_url"] = (
            url_for("download", job_id=job.id) if job.status == "done" else None
        )
        return jsonify(data)

    @app.get("/download/<job_id>")
    def download(job_id):
        job = jobs.get(job_id)
        if job is None or job.status != "done" or job.output_path is None:
            return render_template("missing.html", app_name=APP_NAME, job_id=job_id), 404
        return send_from_directory(
            job.directory,
            job.output_name,
            as_attachment=True,
            mimetype=job.options.get("mime"),
        )

    @app.get("/healthz")
    def healthz():
        """For a host's health check: is ffmpeg actually there?"""
        ffmpeg = core.find_binary("ffmpeg")
        return (
            jsonify(
                {
                    "ok": ffmpeg is not None,
                    "ffmpeg": str(ffmpeg) if ffmpeg else None,
                    "jobs": jobs.count(),
                }
            ),
            200 if ffmpeg is not None else 503,
        )

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_exc):
        limit = app.config["MAX_CONTENT_LENGTH"] // MEGABYTE
        return (
            render_template(
                "index.html",
                **index_context(None, f"that upload is over the {limit} MB limit"),
            ),
            413,
        )

    return app


app = create_app()


def main() -> int:
    """Run the converter: waitress when installed, else Flask's own server."""
    host = os.environ.get("YT2DISC_WEB_HOST", "127.0.0.1")
    port = int_env("PORT", 8000, 1, 65535)
    jobs = app.extensions["yt2disc_jobs"]
    ffmpeg = core.find_binary("ffmpeg")

    print(f"{APP_NAME} - http://{host}:{port}")
    print(f"  ffmpeg  : {ffmpeg or 'NOT FOUND (put it in bin/ or on PATH)'}")
    print(f"  scratch : {jobs.root}")
    print(f"  cap     : {app.config['MAX_CONTENT_LENGTH'] // MEGABYTE} MB per upload")
    print(f"  key     : {'required' if app.config['YT2DISC_WEB_KEY'] else 'not required'}")
    if ffmpeg is None:
        print("  warning : conversions will fail until ffmpeg is available")
    print("  stop with Ctrl+C")

    try:
        from waitress import serve
    except ImportError:
        app.run(host=host, port=port)
        return 0
    try:
        serve(app, host=host, port=port, threads=4)
    except KeyboardInterrupt:
        pass
    finally:
        jobs.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

