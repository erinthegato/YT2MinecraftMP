"""jobs.py - a small background-job registry for the converter.

A conversion takes as long as it takes, so the request that carried the upload
does not wait for it: the request starts a job and hands back its id, and the
page polls that id until the file is ready.

Two deliberate choices:

* jobs live in memory, so a restart forgets them - which is the honest trade
  for a host that may run several copies of the app behind a proxy, and means
  there is no database to lose;
* every file a job owns is deleted when the job ages out, and the uploaded
  original is deleted the moment the conversion succeeds, so the service never
  turns into an accidental file store.
"""

from __future__ import annotations

import collections
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # `import converter` next to this file
    sys.path.insert(0, str(HERE))

import converter  # noqa: E402
import core  # noqa: E402

# How much of ffmpeg's chatter a job keeps for the error box.
LOG_LINES = 40


class Job:
    """One conversion: what was asked for, and how far it got."""

    def __init__(self, job_id: str, source_name: str, options: dict):
        self.id = job_id
        self.source_name = source_name
        self.options = options

        self.status = "queued"  # queued | running | done | error
        self.percent = 0.0
        self.message = "waiting to start"
        self.error = None

        self.created = time.time()
        self.finished = None

        self.directory: Path | None = None
        self.source_path: Path | None = None
        self.output_path: Path | None = None
        self.output_name = converter.output_name(source_name, options)
        self.size = 0
        self.size_text = None
        self.duration = None
        self.duration_text = "--:--"
        self.log_tail: collections.deque = collections.deque(maxlen=LOG_LINES)

    @property
    def done(self) -> bool:
        return self.status in ("done", "error")

    def as_dict(self) -> dict:
        """The job as plain data, for the polling endpoint and the template."""
        return {
            "id": self.id,
            "status": self.status,
            "percent": round(float(self.percent), 1),
            "message": self.message,
            "error": self.error,
            "source_name": self.source_name,
            "output_name": self.output_name,
            "size_text": self.size_text,
            "duration_text": self.duration_text,
            "format": self.options.get("format"),
            "log_tail": list(self.log_tail),
        }


class JobRegistry:
    """Owns the jobs, the worker threads and the scratch disk space."""

    def __init__(self, root=None, slots: int = 2, keep_seconds: float = 7200.0):
        self.root = Path(str(root)) if root else Path(tempfile.gettempdir()) / "yt2disc-web"
        self.root.mkdir(parents=True, exist_ok=True)
        # At most `slots` conversions run at once, so one visitor cannot pin
        # every core on the host.
        self.slots = threading.BoundedSemaphore(max(1, int(slots)))
        self.keep_seconds = float(keep_seconds)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # ---- starting work ---------------------------------------------------
    def submit(self, source_name, stream, options) -> Job:
        """Save the upload and start converting it.

        ``stream`` is any file-like object; Flask hands us a FileStorage, which
        is one, so this stays framework-free.
        """
        suffix = Path(str(source_name or "")).suffix.lower()
        if suffix not in converter.SUPPORTED_INPUTS:
            raise core.InputError(
                f"'{suffix or source_name}' is not a file we can read - upload "
                f"audio ({', '.join(core.AUDIO_EXTENSIONS)}) or a video"
            )

        job = Job(uuid.uuid4().hex[:12], source_name, options)
        directory = self.root / job.id
        directory.mkdir(parents=True, exist_ok=True)
        job.directory = directory
        job.source_path = directory / f"input{suffix}"

        with job.source_path.open("wb") as handle:
            shutil.copyfileobj(stream, handle, length=1024 * 1024)
        if job.source_path.stat().st_size <= 0:
            shutil.rmtree(directory, ignore_errors=True)
            raise core.InputError("the upload arrived empty")

        job.output_path = directory / job.output_name
        with self._lock:
            self._jobs[job.id] = job
        threading.Thread(
            target=self._work, args=(job,), name=f"convert-{job.id}", daemon=True
        ).start()
        return job

    # ---- looking a job up ------------------------------------------------
    def get(self, job_id) -> Job | None:
        with self._lock:
            return self._jobs.get(str(job_id))

    def count(self) -> int:
        with self._lock:
            return len(self._jobs)

    # ---- the worker ------------------------------------------------------
    def _work(self, job: Job) -> None:
        job.status = "running"
        failure = None
        try:
            with self.slots:
                job.message = "converting"
                converter.convert(
                    job.source_path,
                    job.output_path,
                    job.options,
                    log=job.log_tail.append,
                    progress=self._reporter(job),
                    # The upload was saved under a scratch name, so the real
                    # one is handed over for a pack to name its song with.
                    title=job.source_name,
                )
            self._describe(job)
        except core.YT2DiscError as exc:
            failure = str(exc)
        except Exception as exc:  # noqa: BLE001 - a job must never hang
            failure = f"unexpected error: {exc!r}"
        finally:
            self._finish(job, failure)

    def _finish(self, job: Job, failure) -> None:
        """Publish the outcome last.

        A poller breaks the moment it sees ``done``, so the upload has to be
        gone and the size and length filled in *before* that status appears -
        otherwise a page can fetch a job that is finished but still blank.
        """
        if job.source_path is not None:
            try:
                job.source_path.unlink()  # the upload has done its job
            except OSError:  # pragma: no cover - defensive
                pass
        job.finished = time.time()
        if failure is None:
            job.status = "done"
            job.percent = 100.0
            job.message = "ready to download"
        else:
            job.status = "error"
            job.error = failure
            job.message = "failed"
        self.sweep()

    @staticmethod
    def _reporter(job: Job):
        def report(percent: float) -> None:
            job.percent = percent
            job.message = f"converting ({percent:.0f}%)"

        return report

    @staticmethod
    def _describe(job: Job) -> None:
        """Note what was produced.  The status is set later, on purpose."""
        job.size = job.output_path.stat().st_size
        job.size_text = core.human_size(job.size)
        # An addon is a zip, and ffmpeg cannot read a length out of one - so its
        # length is taken from the audio it was built from, which is still here
        # (the upload is only deleted once the job is finished).
        readable = (
            job.source_path
            if converter.is_pack(job.options) and job.source_path
            else job.output_path
        )
        job.duration = core.probe_duration(readable)
        job.duration_text = core.fmt_duration(job.duration) if job.duration else "--:--"

    # ---- housekeeping ----------------------------------------------------
    def sweep(self) -> int:
        """Forget - and delete - jobs that finished long enough ago."""
        now = time.time()
        with self._lock:
            stale = [
                job
                for job in self._jobs.values()
                if job.finished and now - job.finished > self.keep_seconds
            ]
            for job in stale:
                self._jobs.pop(job.id, None)
        for job in stale:
            if job.directory:
                shutil.rmtree(job.directory, ignore_errors=True)
        return len(stale)

    def shutdown(self) -> None:
        """Delete every scratch folder, for when the app stops."""
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            if job.directory:
                shutil.rmtree(job.directory, ignore_errors=True)

