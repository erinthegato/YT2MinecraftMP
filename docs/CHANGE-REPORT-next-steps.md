# yt2disc — next-steps report

**Follows** [`CHANGE-REPORT.md`](CHANGE-REPORT.md) — commit `5501d50`,
`feat(packs): open the player with the /yt2disc:music custom command`.

That report closed with six "Next Steps". This file is the record of doing them.
None of it changes what the player or the converter *does* for a visitor: it is
verification, hardening, and the wiring that keeps the two halves of the project
honest about where their numbers come from.

## 1. The minimum game version and the fallback are documented

`/yt2disc:music` is a custom command, a stable API from Minecraft 1.21 on, and
`MIN_ENGINE_VERSION` in `webapp/packs.py` already said `[1, 21, 0]`. Both
`README.md` and `webapp/README.md` now say so plainly and name the
`/scriptevent yt2disc:menu` fallback explicitly instead of in passing, so a
player on an older game has a documented way in. The fallback runs the same
callback the command does, so the two entry points cannot drift apart.

*Checking the minimum version on a real client is still a manual step* — it
needs the game, not a script — but the constant and both documents now agree,
and the selftest asserts the fallback is present in the generated script.

## 2. Pack-generation regression tests

Two changes, one of them a real fix:

- the pack UUIDs are now **stable**. `_new_uuid()` returned a fresh v4 UUID for
  every manifest field on every build, which makes each build a *different* pack
  as far as the game is concerned — an update installs beside the old one
  instead of over it. `_stable_uuid(role, pack_name)` derives a v5 UUID from the
  namespace and the field's role, so the same pack built on any machine carries
  the same UUIDs.
- `webapp/_selftest.py` grew a pack-regression section: the manifests are
  reproducible, the two halves of a pack use different UUIDs, the same addon
  built twice shares its UUIDs, the song title comes from the source name, and
  the length comes from the source audio — with the addon itself asserted to
  have no readable duration, which is *why* it is read from the source.

## 3. The length probe is strict, and a failure is a real error

`core.probe_duration(..., strict=True)` turns each "no answer" — a missing file,
a probe that times out, a file with no readable duration — into an
`InputError` that names the file. `webapp/converter.py` calls it in strict mode,
so a job whose length cannot be read now fails with a reason instead of
reporting an empty length and a progress bar that never moves.
`webapp/browser_ffmpeg.py` mirrors the behaviour, so a server and a browser fail
the same way.

## 4. Upload validation, and a job timeout

- `JobRegistry` takes `max_upload_bytes`: a saved upload over it is refused with
  a readable message, alongside the existing extension check. Both front-ends
  wire it to `YT2DISC_WEB_MAX_UPLOAD_MB`.
- `JobRegistry` takes `timeout_seconds`, threaded through
  `converter.convert(..., timeout=...)` into `core.run_ffmpeg`, which kills a
  conversion that runs past its deadline and says so. Both front-ends read it
  from the new `YT2DISC_WEB_JOB_TIMEOUT_MINUTES` (default `30`; `0` for no
  limit).

## 5. CI

`.github/workflows/ci.yml` runs `webapp/_selftest.py` and `_e2e/run_e2e.py` on
every push and pull request. A dedicated step checks for ffmpeg directly and
fails there rather than deep inside a conversion, and Tk is installed so the
end-to-end test's desktop-window check runs — or skips cleanly when the runner
has no display.

## 6. Build constants live in one place

`build_layout.py` now holds the build's paths, its two executable names and the
hidden imports; `build_exe.py` and `yt2disc.spec` both import it instead of
restating them, so the icon, the version-info file and the output folder can no
longer drift apart. `packs.pack_folder_names()` is public, so the tests name the
same `_RP`/`_BP` folders the builder writes rather than hard-coding them.

## Checking it

```powershell
py -3 webapp/_selftest.py
py -3 _e2e/run_e2e.py
```

Both were run against these changes; the self-test, including every new check,
passes.
