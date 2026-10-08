# Local Codex 0.161.0 source candidate

The separate [`native-codex-0.161.0.patch`](../scripts/native-codex-0.161.0.patch)
targets upstream commit `979011409de0a60b52f179721948e65531d26144`
(`rust-v0.161.0`). It is a source candidate, not a published, installable Bello
runtime. The existing `native-codex-selection.patch`, 0.155.1 build workflows,
and automatic-download pins remain separate and unchanged.

Use a fresh, isolated checkout of that exact official Git commit, with no account
credentials. The source preparation helper is stdlib-only and makes no network
requests:

```sh
python /absolute/path/to/Bello/scripts/prepare_native_codex_candidate.py identity
python /absolute/path/to/Bello/scripts/prepare_native_codex_candidate.py prepare \
  --source /absolute/path/to/fresh-codex-0.161.0-checkout
```

Preparation refuses another Git revision, a dirty checkout, or linked/missing
patch inputs. A local Git commit made from an extracted source archive is **not**
the official upstream commit and is intentionally refused; archive-based
experiments need their own source/archive provenance receipt. Preparation applies
the named versioned patch and updates only source-less `0.0.0` workspace versions
in `Cargo.lock` to `0.161.0`. Registry and Git dependency versions remain locked.

The JSON output hashes the candidate patch and preparation helpers. This is a
**source-input identity**, not a binary capability manifest or proof of a
successful build. Use Rust 1.95.0, upstream V8 release `rusty-v8-v150.4.0`, and
`--locked` when building. Verify the correct platform's V8 archive and binding
against upstream checksums. Keep the compiled `codex` and `codex-code-mode-host`
together; Linux additionally needs its digest-bound bundled `bwrap`, while Windows
needs both sandbox helper executables. Retain upstream licenses and notices.

Before selecting a candidate with the explicit host binary override, validate
its capabilities and exact executable hash, and run the selection, asynchronous
execution, cancellation, and history proofs against that **same** binary. Native
proofs use a synthetic local provider; they are not live model quality tests.
macOS results do not qualify Windows ACLs or Linux sandboxing. Do not feed a
0.161.0 candidate into the older 0.155.1 packaging/receipt helpers or overwrite
the working global Codex/Bello installation. New published URLs or download pins
must only be added after real platform artifacts and their proofs exist.

## Candidate-only build and qualification

The separate `native-codex-candidate.yml` workflow is limited to
`codex/072-recovery-validation`. The build helper preserves a strict
`native-build-receipt.json` with the exact target, source/toolchain recipe,
normalized lockfile, licenses and every executable hash. Its `proof_status`
remains `not-run`: reusable compiled binaries are not a passing qualification.

The candidate also repairs native conversion of an already-absolute Windows
drive-root URI such as `file:///D:` after lexical parent/config resolution. It
restores only the missing root separator during conversion; native-convention
and encoded-separator rejection still run first, and no filesystem permission
grant changes. The stored hostless drive-root URI also uses one canonical form
across config resolution and native-path reimport. This preserves the strict
lossless permission-path serialization check instead of bypassing it; URI
equality, hashing and sandbox permission grants remain unchanged.
Both native builds run the `codex-utils-path-uri` library tests and the
`codex-protocol` `bello_permission_path_roundtrip` regressions before saving a
build. Windows-specific root/config/parent and permission-profile roundtrips
therefore run on Windows, not merely as portable source-contract tests. A local
portable pass is not a native Windows runtime qualification.

Cargo intermediate caches may reuse an older recipe on the same target, with
`cargo --locked` rebuilding affected inputs. Ready-candidate caches never use
that fallback: their exact source/build key and complete receipt must match.

On the matching native Linux or Windows x64 host, qualify that explicit directory:

```sh
python scripts/verify_native_codex_candidate.py --candidate native-candidate \
  --target linux-x64 --output-dir candidate-proof --modernbert
```

Use `--target windows-x64` on Windows. The supplied executable must report exactly
`codex-cli 0.161.0`. The wrapper does not use the published native installer or
change download pins. It checks the full build receipt before and after each
proof, binds proof-script hashes, and requires nine selection cases, fourteen
async cases including three cold concurrency attempts, persistent history, the
bundled Linux sandbox where applicable, and six actual CPU ModernBERT cases.
ModernBERT uses the public pinned bundle with authentication disabled; only that
download may contact an external model repository. Provider proofs use synthetic
loopback requests, empty homes, no account credentials, and rejecting proxies.

The public command runs the proof worker inside the production process guardian.
Final success additionally requires one successful worker, confirmed full-tree
cleanup and no Stop/retry. Thus native Windows sandbox helpers are exercised
inside the outer nonbreakaway Job, not only as unguarded processes.

`candidate-proof/qualification.json` is the aggregate gate. Bounded supporting
reports live under `candidate-proof/worker/`; a passing worker report alone is
not sufficient. Do not upload the guardian control directory or model/home
caches. Local mocked wrapper tests prove validation behavior, not native-platform
execution, learned-model performance, or release readiness.
