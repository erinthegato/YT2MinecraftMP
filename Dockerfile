# yt2disc converter - the hosted half, as a container image.
#
# Only needed by hosts that build an image (Kubernetes, any `docker run`), or by
# anyone who would rather run the Flask front-end in a container than on the
# host.  A Procfile host can provide ffmpeg itself and skip this file: Render's
# native Python runtime already ships ffmpeg, and Heroku reads the Aptfile in the
# repository root.
#
# The point of this image is one thing: put ffmpeg on PATH, which is exactly
# where core.find_binary() looks first outside Windows.  The Hugging Face Space
# this repository is set up for runs the Gradio page instead - `sdk: gradio` and
# `app_file: app.py` in the block at the top of README.md, with ffmpeg from
# packages.txt - so that deployment never builds this image at all.
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    YT2DISC_WEB_HOST=0.0.0.0 \
    YT2DISC_WEB_DATA=/tmp/yt2disc \
    PORT=8000

# ffmpeg does every conversion and is not a Python package, so it comes from
# the distribution.  Everything else the converter needs is pure Python.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first: they change far less often than the code, so Docker can
# keep this layer cached across rebuilds.
COPY webapp/requirements.txt webapp/requirements.txt
RUN pip install --no-cache-dir -r webapp/requirements.txt

# core.py is the shared engine the converter borrows; webapp/ holds the rest
# (app, converter, jobs, templates, static).
COPY core.py ./
COPY webapp/ webapp/

# The converter only ever writes scratch files, so it does not need to own the
# code: mount a volume over the scratch folder if the host has one.  The uid is
# 1000 because a Hugging Face Space runs its container as that user, and
# matching it keeps $HOME writable there; Docker and Kubernetes do not mind.
RUN useradd --create-home --uid 1000 yt2disc
USER yt2disc

EXPOSE 8000

# Mirrors webapp/Procfile, in exec form so the process receives SIGTERM
# directly.  /healthz answers 503 while ffmpeg is missing, so a broken deploy
# is visible to the host instead of failing quietly for every visitor.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=5)"

CMD ["python", "-m", "webapp.app"]