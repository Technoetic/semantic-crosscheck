---
description: "Install the pinned Cube adapter once per user (npm ci --ignore-scripts from the committed lockfile plus a SHA-256-verified native module)"
argument-hint: "[--home DIR] [--native-file PATH] [--force] [--out DIR] [--quiet]"
---

Run the `cube-install` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not
shell code: pass each one as its own argument and never splice them into a shell string.

## When to use

- Once per user and Cube release, before the first [/xc-cube](xc-cube.md), and again after a plugin
  update that changed the adapter files.
- When [/xc-doctor](xc-doctor.md) with `--require cube` reports `cube:adapter` or `cube:native` not
  ready.
- It needs Node.js 22 or later with `npm` on `PATH`, and network access to the npm registry and to the
  Cube release downloads on GitHub (or a native tarball downloaded beforehand, see `--native-file`).

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" cube-install
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" cube-install --native-file cube-native.tar.gz
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" cube-install --force
```

- The adapter's npm packages are installed with `npm ci --ignore-scripts` from the lockfile committed
  in `skills/semantic-crosscheck/adapters/cube/`, so no package install script runs. npm uses its own
  proxy settings.
- The Cube native module for this platform is downloaded over https from the Cube GitHub release
  (honouring `HTTPS_PROXY`) and accepted only if its size and SHA-256 match `native-sha256.json`.
  `--native-file` uses a tarball that is already on disk; it is verified the same way.
- On Linux the glibc version is checked before anything is installed: the pinned native modules need
  glibc 2.34 or later on x64 and 2.30 or later on arm64.
- Install directory: `--home DIR`, else `SXC_CUBE_HOME`, else a per-user cache
  (`%LOCALAPPDATA%\semantic-crosscheck\cube-<version>` on Windows,
  `$XDG_CACHE_HOME/semantic-crosscheck/cube-<version>` or `~/.cache/semantic-crosscheck/cube-<version>`
  elsewhere; about 355 MB). The other stages look only in `SXC_CUBE_HOME` or the default location, so an
  install made with `--home DIR` needs `SXC_CUBE_HOME=DIR` for every later stage; the `next:` line says
  so.
- An existing install that still matches the pins and this plugin's adapter files is kept
  (`cube_install:already_installed`); one made from other adapter files (an older plugin) is reinstalled
  without `--force`. `--force` reinstalls in any case.
- No configuration is needed. The result goes to `.sxc/cube_install/result.json` in the working
  directory (or next to `--config`, or under `--out`), with `npm-ci.log` and `smoke.log` beside it.
  Progress lines go to stderr unless `--quiet`.
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

Exit code 0 and `[sxc cube_install] PASS` with `cube_install:npm_ci`, `cube_install:native_sha256`,
`cube_install:extract` and `cube_install:smoke` passing (the smoke test loads the native module and the
server packages), or `cube_install:already_installed` when the existing installation still matches.
[/xc-doctor](xc-doctor.md) with `--require cube` then passes `cube:node`, `cube:adapter` and
`cube:native`.

## Failures

- `cube_install:native_sha256` fails (size or SHA-256 mismatch): stop and report it. Never bypass the
  check, never replace a hash in `native-sha256.json` with one computed from the download, and never fall
  back to an unverified file.
- `cube_install:extract` fails: the tarball layout is unexpected (an extra member, a link, a path outside
  the package); report it and do not work around the check.
- `cube_install:smoke` fails: read `.sxc/cube_install/smoke.log` (it holds the loader error, for example
  a missing `GLIBC_` symbol), check that `node` is version 22 or later and built for the machine's
  architecture, then rerun with `--force`.
- `cube_install:npm_ci` fails: read `.sxc/cube_install/npm-ci.log`, check the Node.js and npm versions
  and any proxy settings. Do not switch to `npm install` (it ignores the lockfile) or drop
  `--ignore-scripts`.
- The steps after a failed one are `not_run`.
- Exit code 2: `--native-file` names a file that does not exist (also when an install already exists),
  `--home` is not a directory, or it is neither empty nor an adapter installation.
- Exit code 3: an unsupported platform (Cube's native module is pinned only for the platforms listed in
  `native-sha256.json`; Linux needs glibc, musl is not supported), a glibc that is too old, `node` or
  `npm` missing from `PATH` or Node.js older than 22, or `node` built for another architecture than the
  machine. On an unsupported platform report `cube` and the Cube parts of `verify` as not proven; the
  other stages still work.
