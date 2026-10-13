---
description: "Install the pinned Cube adapter once per user (npm ci --ignore-scripts from the committed lockfile plus a SHA-256-verified native module)"
argument-hint: "[--home DIR] [--native-file PATH] [--force]"
---

Run the `cube-install` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not
shell code: pass each one as its own argument and never splice them into a shell string.

## When to use

- Once per user and Cube release, before the first [/xc-cube](xc-cube.md).
- When [/xc-doctor](xc-doctor.md) with `--require cube` reports `cube:adapter` or `cube:native` missing.
- It needs Node.js 20 or later with `npm` on `PATH`, and network access to the npm registry and to the
  Cube release downloads on GitHub (or a native tarball downloaded beforehand, see `--native-file`).

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" cube-install
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" cube-install --native-file cube-native.tar.gz
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" cube-install --force
```

- The adapter's npm packages are installed with `npm ci --ignore-scripts` from the lockfile committed
  in `skills/semantic-crosscheck/adapters/cube/`, so no package install script runs.
- The Cube native module for this platform is downloaded from the Cube GitHub release and accepted only
  if its SHA-256 matches `native-sha256.json`. `--native-file` uses a tarball that is already on disk;
  it is verified the same way.
- Install directory: `--home DIR`, else `SXC_CUBE_HOME`, else a per-user cache
  (`%LOCALAPPDATA%\semantic-crosscheck\cube-<version>` on Windows,
  `$XDG_CACHE_HOME/semantic-crosscheck/cube-<version>` or `~/.cache/semantic-crosscheck/cube-<version>`
  elsewhere). `--force` reinstalls over an existing installation.
- No configuration is needed. The result goes to `.sxc/cube_install/result.json` in the working
  directory (or next to `--config`, or under `--out`).
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

Exit code 0 and `[sxc cube_install] PASS` with `cube_install:npm_ci`, `cube_install:native_sha256`,
`cube_install:extract` and `cube_install:smoke` passing (the smoke test loads the native module and the
server packages), or `cube_install:already_installed` when the existing installation still matches the
pins. [/xc-doctor](xc-doctor.md) with `--require cube` then passes `cube:node`, `cube:adapter` and
`cube:native`.

## Failures

- `cube_install:native_sha256` fails (size or SHA-256 mismatch): stop and report it. Never bypass the
  check, never replace a hash in `native-sha256.json` with one computed from the download, and never fall
  back to an unverified file.
- Unsupported platform (exit code 3): Cube's native module is pinned only for the platforms listed in
  `native-sha256.json`; Linux needs glibc (musl is not supported). Report `cube` and the Cube parts of
  `verify` as not proven; the other stages still work.
- `cube_install:npm_ci` fails: read `.sxc/cube_install/npm-ci.log`, check the Node.js and npm versions
  and any proxy settings. Do not switch to `npm install` (it ignores the lockfile) or drop
  `--ignore-scripts`.
- Exit code 2: `--home` is not an empty directory or an existing adapter installation, or
  `--native-file` is not a file.
- Exit code 3: `node` or `npm` is missing or too old (Node.js 20 or later), or `node` was built for
  another architecture than the machine.
