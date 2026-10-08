# Source organization and compatibility boundaries

Bello's production Python package is `supervisor/`. `supervisor/main.py` owns
CLI entry points; `runtime/` owns provider routing, transports, tool execution,
sandbox integration and engine installation. The package name is historical
and remains part of the public import contract.

## Controller coordination and state

`controller.py` coordinates incoming events, approvals and the run. Its
`controller_parts/` package implements composed services, each with a separate
slotted state record and an explicit coordinator port:

| Component | Responsibility and state owner |
| --- | --- |
| `commands.py`, `command_parsing.py` | Command output, validation and inspection ledgers, execution-position parsing |
| `evidence.py`, `evidence_rules.py` | Changed-file observations, review packets, private-input filtering and evidence limits |
| `fingerprints.py`, `adversary_workspace.py` | Stable filesystem fingerprints and isolated adversary workspace preparation |
| `children.py` | Child and reviewer registries, ancestry, allowed profiles and cleanup |
| `runtime_review.py`, `runtime_rules.py` | Queued runtime reviews, trigger acknowledgements and stale-result handling |
| `completion.py`, `completion_rules.py` | Readiness, completion returns, adversary budgets and review outcomes |
| `coder_lifecycle.py`, `lifecycle_rules.py` | Coder delivery barriers, interruption, revision switching, restart and recovery |
| `shutdown.py` | Terminal finalization, report publication and cleanup |
| `settings.py`, `preflight.py`, `event_protocol.py` | Configuration, readiness checks and event normalization |

`interfaces.py` restricts the coordinator names each service may read or write.
Cross-service operations use declared callbacks; per-run state has one owner.
The public controller's old attributes are descriptors into that owner's state,
not synchronized copies. Lazy construction preserves legacy `__new__` fixtures
and the difference between absent attributes and initialized values. Async
methods remain async methods and retain inspectable signatures.

`compat.py` resolves historical module-level dependencies at call time. This is
necessary because tests and embedders patch `supervisor.controller` helpers,
clocks and constructors. Re-exporting an already-bound implementation would
silently change those patches. This compatibility namespace contains no run
state and does not replace the explicit state/port interfaces.

## Process recovery boundaries

`controller_recovery.py` owns durable run identity, exclusive controller ownership,
checkpoint validation and same-session continuation. `watchdog.py` owns the bounded
restart policy; `process_fence.py` owns live descendant-process containment. The
watchdog cannot authorize continuation unless the controller's saved state and
the guardian's cleanup evidence both permit it. `snapshot_recovery.py` persists
and restores trusted workspace authority; `snapshot_transaction.py` additionally
records final application boundaries so an uncertain patch is not replayed.

These components do not reset model/review budgets or create a replacement task.
See [crash recovery](crash-recovery.md) for unsupported/ambiguous states and the
explicit macOS automatic-continuation limitation.

## Snapshot authority and transactions

`workspace_snapshot.py` retains public snapshot records, constructors, constants
and helper import paths. `SnapshotServices` is the typed live dependency port
passed to the implementation modules. The module split follows responsibility:

- `snapshot_construction.py`: create and initialize disposable workspaces.
- `snapshot_state.py`: manifests, hashes, candidate selection and comparison.
- `snapshot_security.py`: authority, target and link validation.
- `snapshot_transaction.py`: the sole patch-back transaction, including backups,
  checks, application, verification and rollback.
- `snapshot_git.py`: controlled Git environment and index operations.
- `snapshot_runtime.py`, `snapshot_lifecycle.py`: runtime exposure, repair,
  integrity checks, recovery and cleanup.
- `snapshot_filesystem.py`, `snapshot_windows.py`: safe filesystem primitives
  and Windows-specific guards/watchers.

Construction initializes the snapshot's baselines. Runtime controls own later
manifest/control changes. Comparison reads those records; only the transaction
writes a candidate back to the original workspace. The ordering of trusted Git
configuration restoration, Windows pre-Git auditing, path checks, symlink checks,
backup, apply and verification remains explicit. Failure at any boundary must
retain the original rollback semantics.

## Policy decisions

`policy.py` retains `PolicyEngine`, ordered decisions and the public compatibility
surface. `policy_types.py` defines shared parsed-command and analysis records.
`policy_parsing.py` handles conservative shell and operand parsing;
`policy_paths.py` handles path identity and scope; `policy_analysis.py` classifies
segments and risk; `policy_rules.py` contains denial predicates. `PolicyEngine`
owns workspace, roots and shell configuration. These helpers own no mutable
engine state. Runtime helper lookup continues through the public policy module
so patched shell/platform boundaries still apply to nested calls.

## Configuration editor

`config_editor.py` owns the active editor session, key dispatch and compatibility
API. Its frozen `EditorState` records represent navigation and edits. The
remaining responsibilities have explicit boundaries:

- `config_editor_catalog.py`: discover and present provider-qualified models.
- `config_editor_parameters.py`: define rows and validate live selections.
- `config_editor_updates.py`: transform state, apply selections and save changes.
- `config_editor_types.py`: shared records and constants.
- `config_editor_terminal.py`, `config_editor_fragments.py`: terminal widths,
  truncation and styled fragments.
- `config_editor_widgets.py`, `config_editor_rendering.py`: compose controls and
  the screen without owning persisted configuration.

Catalog scratch data stays separate from the active configuration. A refresh
does not rewrite a saved model. Helpers resolve historical dependencies through
the public editor module so existing patches and embedders retain their effect.

## Pi discovery and execution

`runtime/pi.py` and `pi_worker/src/agent-directory.mjs` implement matching agent
directory precedence. `catalog.mjs` gives discovery read-only local snapshots;
session creation uses separate normal SDK stores so legitimate credential
renewal remains an execution operation. A session's model and credentials come
from the same execution runtime; catalog drift is rejected. Explicit refresh
validates a replacement before publishing it, rejects overlapping turns/session
creation, and reports its source and load time without claiming remote
freshness. See [runtime setup](runtime.md#sign-in-and-choose-models).

Validate SDK upgrades in an isolated copy of the current worker with its own
dependency tree. Record the package versions and import paths resolved from
that copy before tests. Do not replace a source dependency link managed by the
workspace's integrity controls. Python integration fixtures accept
`BELLO_TEST_PI_WORKER_DIR` to select the copy; Node schema tests accept
`BELLO_TEST_PYTHON` to use the isolated Python test environment.

## Tests and shared support

`tests/` contains collected offline regression tests. `tests/test_bello_state.py`
now contains persistence/report tests; controller cases are split into
`test_controller_*.py` files covering profiles, evidence, validation parsing,
command events, Windows command evidence, readiness, runtime queues/triggers,
reviewer sessions, revision/restart/interrupt lifecycle, approvals, completion,
adversary behavior and failure/terminal recovery.

`tests/support/controller.py` holds reusable synthetic sessions, controller
builders and packet helpers. It is not a collected test module. The four suites
that previously imported helpers from `test_bello_state.py` now import this
support module. `tests/conftest.py` registers the shared POSIX command fixture.
The split preserves the original 257 functions and all 420 parametrized cases;
only the source-scanning test's input list was extended to cover the new
controller implementation files. The Windows executable fixtures remain under
`tests/windows_fixtures/`.

The Pi worker's `test/` directory contains Node tests and synthetic providers.
The Python Pi/pipeline integration tests use a local synthetic model endpoint
with the real SDK and sandbox, without paid provider turns.

## Path dependencies and packaging

| Path or consumer | Dependency |
| --- | --- |
| `pyproject.toml` package discovery | All `supervisor*` packages; implementation subpackages need `__init__.py` |
| Python package data | Prompt TOML, runtime tools JSON/native notices, Pi worker entry points, source `.mjs`, manifests and license notices |
| `MANIFEST.in`, `setup.py` | Windows native helper sources/build inputs; the platform-specific build remains unchanged |
| `supervisor/runtime/install.py` | Pi worker manifest/lock and entry points relative to the installed Python package |
| `tests/test_controller_validation_evidence.py` | Scans `controller.py`, all `controller_parts/*.py` and prompt TOML |
| `.github/workflows/` | Full Python discovery, Pi `npm test`, explicit offline integration paths and native-platform jobs |
| `plugins/bello/skills/` | Repository copies of advisor/delegation skill code and references; separate from the Python wheel |
| `scripts/` | Maintainer builds and explicit local verification; not imported as production implementations |
| `native/windows-sandbox/` | Rust/native source and platform verification fixtures; platform artifacts remain separate |

No workflow named individual tests inside the old state file, so its split does
not require a workflow path migration. The wheel and sdist must include every
new production module. Tests/shared support and local receipts are not runtime
package dependencies.

## Documentation, experiments and local receipts

`docs/` contains maintained technical documentation; `docs/assets/` and the root
media files are unchanged. Benchmark CSVs, plotting scripts and existing
experiments retain their original paths. Moving them later would require a
separate inventory of plotting, documentation and personal workflow consumers;
this task does not move or delete them.

`.test-venv/`, `.test-tmp/` and `.verification/` are ignored local test tools,
temporary workspaces and receipts. Git ignore is **not** a verification snapshot
exclusion: ignored and untracked files may be relevant submitted inputs and are
copied for review. In self-hosted runs, keep pytest `--basetemp` and `TMPDIR`
outside the submitted checkout; keep test dependencies and build receipts in
separate staging directories too. Tests creating FIFOs or unreadable permission
fixtures must clean up those owned entries even when assertions fail. Verification
fails closed on uncopyable inputs; it never changes source permissions or silently
omits entries. Set `GIT_CEILING_DIRECTORIES` to the temporary fixture root so
non-repository fixtures cannot inherit an outer checkout. Run source tests with
`PYTHONPATH` pointing to this checkout, and unset `BELLO_SKIP_UPDATE_CHECK` for
update-gate/full-suite checks.
The installed Bello runner, global tools, plugin cache and active `.supervisor`
state are separate from edited source and must not be updated during review.
