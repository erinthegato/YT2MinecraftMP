@echo off
rem Stand-in for ffmpeg used by the end-to-end test (see run_e2e.py).
rem YT2DISC_PYTHON must point at a Python interpreter; run_e2e.py sets it.
if not defined YT2DISC_PYTHON (
  echo fake ffmpeg: YT2DISC_PYTHON is not set 1^>^&2
  exit /b 2
)
"%YT2DISC_PYTHON%" "%~dp0fake_ffmpeg.py" %*

