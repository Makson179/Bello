# Native Codex log selection

Subscription Codex runs through its native app-server. Disabling log distillation
uses the normal native tool results. Enabling it requires a patched Codex executable:
stock app-server notifications cannot replace results in the model's conversation.
The other provider engines do not need this patch.

Bello does not replace the user's global `codex`, modify its configuration, or fall
back to Pi when selection is unavailable. The model bundle and native executable
are separate downloads. On platforms listed in
[`native_codex_install.py`](../supervisor/runtime/native_codex_install.py), an
approved native-Codex run with log distiller enabled prepares both automatically.
Installing `Bello[log-distiller]` installs the inference dependencies; the native
helper is fetched when that execution engine is first needed, not by pip.

## Automatic preparation

The native archive and its capability manifest are pinned by SHA-256. The helper,
code-mode host and license files are cached under `~/.bello/runtime/native-codex/`
(or the host's `BELLO_RUNTIME_DIR`). A complete verified cache works offline. Each
version has its own private directory; changed or unsafe cached files cause an
error rather than replacement underneath a running process.

The first download finishes before app-server starts, outside model-RPC timeouts.
Codex runs without distiller use the ordinary executable. Claude/Pi-only runs do
not download this helper. Unsupported platforms require an explicit compatible
build; Bello does not silently turn distillation off or change the provider.

## Select an installed build

To override the pinned download with a custom build, set these host environment
variables before starting Bello:

```sh
export BELLO_CODEX_BINARY=/opt/bello-native/bin/codex
export BELLO_CODEX_SELECTION_MANIFEST=/opt/bello-native/selection-manifest.json
```

The first variable selects the executable for subscription Codex. The second is
optional for a binary whose exact SHA-256 is pinned in Bello; it supplies a
capability declaration for a separately built executable. It is not a project
configuration setting or an instruction the coder can change.

The manifest must be a local JSON object, at most 64 KiB:

```json
{
  "binary_sha256": "<actual SHA-256 of the installed codex executable>",
  "version": "0.153.4",
  "protocol": 1,
  "feature": "bello_native_selection",
  "transport_timeout_seconds": 315
}
```

Bello checks the executable hash, protocol, feature, transport deadline, and the
actual `codex features list` output before enabling selection. A manifest is an
operator's explicit declaration, not a signature or proof of correctness; only
point it at a build whose source and tests you have verified. Stock Codex, a stale
manifest, and the older 135-second build fail with an actionable error before a
distilled coder turn starts. D-off does not read this manifest or run these checks.

The tested Linux x86-64 Codex 0.153.4 executable has SHA-256
`49f183a9cbd91a7e87d0f44c27d1aa60f150359c44eab69084127888bd32dc6c`.
The bridge uses Unix sockets. Automatic native selection is available for macOS
Apple Silicon (arm64); the archive contains both native executables and their
license notices. The macOS executable has SHA-256
`e01aceea077958b9d9bc3645f3dbd6ab8d86f4b45e6e0a5391a20f67175a5656`.
Linux currently uses the explicit compatible-build option above. The Windows
implementation uses an authenticated loopback transport instead of Unix sockets;
its manifest must additionally declare `"transports": ["tcp-hmac-v1"]`.
The Windows build/provider-proof workflow below must pass before a Windows
archive is added to the automatic-download table. This is not a universal
prebuilt package, and an unverified Windows binary is not a supported release.

### Windows build and verification

The `Native Codex Windows proof` workflow has separate build and proof jobs on
Windows Server 2025. The reusable build workflow saves a **CI-only, unverified
candidate** before any proof runs. Its exact cache key covers the upstream build
recipe, source preparation and native patch, not Python runtime code or tests.
On a matching ready-binary cache hit, source checkout, Rust setup and compilation
are skipped. Missing/evicted caches or changed native inputs require a build.
Every restored candidate's input identity and file hashes are checked before use.

The proof job downloads that run's candidate, runs the installer and bridge
regressions, checks all nine offline provider-boundary cases, then installs the
packaged bundle and repeats those cases. A failed proof can be rerun using
GitHub's **Re-run failed jobs** without repeating the successful build job.
Only successful proofs produce the verified bundle; a cached candidate is not a
supported release. Neither workflow publishes a release or modifies the user's
global Codex. The Windows archive includes `codex.exe`, `codex-code-mode-host.exe`,
`codex-command-runner.exe`, and `codex-windows-sandbox-setup.exe` together.

For a verified Windows artifact, the explicit-build configuration is:

```powershell
$env:BELLO_CODEX_BINARY = 'C:\bello-native\bin\codex.exe'
$env:BELLO_CODEX_SELECTION_MANIFEST = 'C:\bello-native\selection-manifest.json'
```

The bridge listens only on `127.0.0.1` and uses a new 256-bit secret for each run.
Both sides authenticate using fresh challenges before command output is sent.
The secret is passed only to the trusted native app-server, never stored in a
log/configuration file, and removed from tool subprocess environments even when
a shell policy requests full environment inheritance. A failed handshake retains
the original native output; it never counts as successful compression.

## Build from source

The source patch is [native-codex-selection.patch](../scripts/native-codex-selection.patch).
It targets the official `rust-v0.153.4` source, not an arbitrary current checkout.
It changes only the optional command/poll focus field, local selection hook and
related tests. Execution, native permissions, policy hooks, model routing and
the existing output metadata stay native. It does not disable sandboxing.

Use a separate build directory, with no user account credentials mounted. Install
the upstream Rust/Cargo build prerequisites first. The validated build used
Rust 1.95.0 and Linux x86-64 with glibc 2.35, Clang, lld, libclang, libcap,
OpenSSL development headers, `just`, and `cargo-nextest`.

```sh
git clone --branch rust-v0.153.4 --depth 1 https://github.com/openai/codex.git codex-bello-selection
cd codex-bello-selection
git apply --check /absolute/path/to/Bello/scripts/native-codex-selection.patch
git apply /absolute/path/to/Bello/scripts/native-codex-selection.patch
cd codex-rs
```

The upstream tag's workspace package versions may need alignment: in the tested
source, `Cargo.toml` used 0.153.4 while workspace-only `Cargo.lock` entries used
0.0.0. If Cargo reports that mismatch, run `cargo update --workspace` and inspect
the lock diff. Only source-less workspace package versions may change from
0.0.0 to 0.153.4; do not accept registry/git dependency changes. Use `--locked`
for the builds and tests after this alignment.

V8 is also needed by Codex code mode. For this tag, the lockfile uses V8 150.4.0.
To avoid compiling it, obtain the official `rusty-v8-v150.4.0` release's
`ptrcomp_sandbox_release_x86_64-unknown-linux-gnu` archive and Rust binding,
verify them against the release's corresponding `.sha256` file, and set
`RUSTY_V8_ARCHIVE` and `RUSTY_V8_SRC_BINDING_PATH` to those local files.

Build the native sandbox helper before Codex so the helper's exact digest is
compiled into Codex:

```sh
export CARGO_BUILD_JOBS=2
export CARGO_PROFILE_RELEASE_DEBUG=0
export CARGO_PROFILE_RELEASE_LTO=false
export CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16
cargo build --locked --release --target x86_64-unknown-linux-gnu --bin bwrap
strip --strip-debug --strip-unneeded target/x86_64-unknown-linux-gnu/release/bwrap
export CODEX_BWRAP_SHA256="$(sha256sum target/x86_64-unknown-linux-gnu/release/bwrap | cut -d ' ' -f 1)"
cargo build --locked --release --target x86_64-unknown-linux-gnu --bin codex --bin codex-code-mode-host
just test --locked --release --target x86_64-unknown-linux-gnu -p codex-core --lib native_selection
```

Keep `codex`, `codex-code-mode-host`, and the exact hashed `bwrap` together in a
dedicated `bin` directory. Keep the upstream license/notices in that installation.
Check all binaries' dynamic-library dependencies on the destination, run
`codex --version` and `codex features list`, and create the manifest using the
final installed executable's hash. A fresh build need not be byte-identical to
Bello's pinned artifact, which is why the explicit manifest option exists.

## Delivery and verification

Only coder threads with distillation enabled receive the short focus instruction
and native feature flag, including resumed/revision coder work. Other roles do
not. The private channel returns only selected text; no recovery handle or extra
metadata is appended to the coder's output. The 300-second selector deadline
fits inside the 315-second native transport deadline. Missing focus, worker
failure, or a non-shorter result retains the normal native output.

The patch's Rust tests cover native direct output, code-mode output, polling's
original command, missing/malformed replies, thread-specific focus schemas and
a response after 301 virtual seconds. The validated 0.153.4 build additionally
passed a synthetic local-provider check that inspected the next model request:
D-off delivered the original text and D-on delivered the exact selector text for
both direct commands and code-mode polling, with exit code preserved. Rebuilds
should repeat that boundary check before being used for comparisons; a feature
listing alone is not evidence of model-visible delivery.

For macOS arm64, `scripts/verify_native_codex_selection.py` exercises nine
offline provider-boundary cases: direct command, code-mode and polling with
selection on/off, missing focus, a custom task file, and CLI help. It uses
synthetic logs and a local provider, with no account credentials or paid model
calls. All nine cases pass for the macOS executable pinned above. The helper's
transport checks are separate from the learned selector's quality evaluation.

The [official app-server protocol](https://learn.chatgpt.com/docs/app-server#protocol)
remains the external native transport. The private selection channel is a Bello
extension, not an official OpenAI protocol capability.

## License

OpenAI Codex is Apache-2.0 licensed. The patch includes the license text and
upstream attribution; retain the upstream `LICENSE` and `NOTICE` when distributing
patched builds, and mark them as modified. The native binaries' dependencies
also retain their respective licenses. Bello's own code license is unchanged.
