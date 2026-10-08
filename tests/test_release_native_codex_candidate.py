"""Release contract tests. Synthetic fixtures do not claim native execution."""
import asyncio
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

from scripts import release_native_codex_candidate as release
from scripts import verify_native_codex_candidate as common


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def case(name):
    return {"case": name, "passed": True, "provider_errors": [], "provider_requests": 2,
            "external_proxy_requests_forwarded": 0, "error": None,
            "exact_model_visible_output": True, "focus_and_command_correct": True,
            "windows_filesystem_sandbox_enforced": True,
            "real_model_selection": True, "model_bypassed": True}


def proof_report(name, binary):
    base = {"passed": True, "paid_model_calls": 0}
    if name == "selection":
        return {**base, "schema": "bello.native-selection-provider-proof.v1", "binary_sha256": release.sha256(binary),
                "cases": [case(value) for value in sorted(common.SELECTION_CASES)]}
    if name == "async":
        rows = [case(value) for value in sorted(common.ASYNC_CASES)]
        for row in rows:
            if row["case"].startswith("async_and_selection_"):
                row["async_tools"] = True
        return {**base, "schema": "bello.native-async-smoke.v1", "on_off_native_instructions_identical": True, "results": rows}
    if name == "history":
        return {**base, "schema": "bello.native-persistent-history-smoke.v1", "provider_passed": True,
                "exact_selected_history_match": True, "live_coder_task": False}
    if name == "modernbert":
        return {**base, "schema": "bello.candidate-real-modernbert-proof.v1", "binary_sha256": release.sha256(binary),
                "device": "cpu", "model_files_unchanged": True,
                "cases": [case(value) for value in sorted(common.MODERNBERT_CASES)]}
    from scripts.build_native_codex_linux import SANDBOX_SCHEMA
    return {**base, "schema": SANDBOX_SCHEMA, "binary_sha256": release.sha256(binary),
            "bwrap_sha256": release.sha256(binary.parent / "codex-resources/bwrap"),
            "inside_write_succeeded": True, "outside_write_denied": True, "new_user_namespace": True,
            "tampered_bwrap_exit_code": 8, "system_bwrap_on_path": False}


def write_proof(name, binary, output):
    value = proof_report(name, binary)
    put(output / "report.json", value)
    if name in {"selection", "modernbert"}:
        for row in value["cases"]:
            put(output / row["case"] / "result.json", row)
    elif name == "history":
        put(output / "case/result.json", case("persistent_direct"))
    elif name == "async":
        for label, original in (("async_and_selection_direct", "direct_on"), ("async_and_selection_code", "code_on")):
            put(output / label / "result.json", case(original))
    return value


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    def make(target="linux-x64"):
        base = tmp_path / target
        candidate, qualification = base / "candidate", base / "qualification"
        candidate.mkdir(parents=True)
        identity = {"version": release.VERSION, "target": target, "build_key": "b" * 64}
        for name in (release.payload(target) - {"selection-manifest.json"}) | {"Cargo.lock"}:
            path = candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"unit fixture: " + name.encode())
            path.chmod(0o755 if name in release.executables(target) else 0o644)
        put(candidate / "BUILD-INFO", {"native_build_identity": identity, "cargo_lock_sha256": release.sha256(candidate / "Cargo.lock")})
        files = {p.relative_to(candidate).as_posix(): release.sha256(p) for p in candidate.rglob("*") if p.is_file()}
        receipt = {"schema": "unit-only-build", "identity": identity, "files": files, "proof_status": "not-run"}
        put(candidate / "native-build-receipt.json", receipt)
        def verify(actual_target, directory, **kwargs):
            assert actual_target == target and kwargs.get("restore_executable_modes") is False
            if release.read_json(directory / "native-build-receipt.json") != receipt:
                raise ValueError("Changed fixture build receipt")
            if set(p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()) != set(files) | {"native-build-receipt.json"}:
                raise ValueError("Changed fixture build inventory")
            if any(release.sha256(directory / name) != digest for name, digest in files.items()):
                raise ValueError("Changed fixture build bytes")
            return deepcopy(receipt)
        build = SimpleNamespace(verify_build=verify, identity=lambda _: deepcopy(identity))
        proof = SimpleNamespace(SCHEMA="unit-only-qualification", proof_inputs=lambda: {"fixture.py": "a" * 64})
        monkeypatch.setattr(release, "_modules", lambda _: (build, proof, common))
        monkeypatch.setattr(release, "packaging_inputs", lambda _: {"fixture-packager": "c" * 64})
        binary = candidate / ("bin/codex.exe" if target == "windows-x64" else "bin/codex")
        worker = {"schema": proof.SCHEMA, "target": target, "passed": True, "published": False,
            "phase": "complete", "candidate_unchanged": True, "modernbert_requested": True,
            "paid_model_calls": 0, "external_provider_requests_forwarded": 0,
            "build_identity": identity, "candidate_files": files, "proof_inputs": proof.proof_inputs(),
            "build_receipt_sha256": release.sha256(candidate / "native-build-receipt.json"),
            "binary_sha256": release.sha256(binary), "proofs": {},
            "native_version": {"version": release.VERSION, "exit_code": 0,
                "binary_sha256": release.sha256(binary), "stdout_sha256": "d" * 64}}
        if target == "darwin-arm64": worker["full_tree_recovery_proven"] = False
        for name in sorted(release.proof_names(target)):
            directory = qualification / "worker" / name
            report = write_proof(name, binary, directory)
            worker["proofs"][name] = {"passed": True, "report": f"{name}/report.json",
                "sha256": release.sha256(directory / "report.json"), "case_results": release._case_hashes(directory, name, common),
                "cases": len(report.get("cases", report.get("results", [report])))}
        put(qualification / "worker/qualification.json", worker)
        aggregate = deepcopy(worker)
        aggregate.update(worker_passed=True, worker_report="worker/qualification.json",
            worker_report_sha256=release.sha256(qualification / "worker/qualification.json"),
            guardian={"watchdog_exit_code": 0, "receipts": [{"exit_code": 0, "fenced": True, "stopped": False,
                "owner_pid": 123, "scope": "groups" if target == "darwin-arm64" else "tree"}]})
        aggregate["proofs"] = {name: {**item, "report": "worker/" + item["report"]} for name, item in worker["proofs"].items()}
        put(qualification / "qualification.json", aggregate)
        return SimpleNamespace(target=target, candidate=candidate, qualification=qualification, receipt=receipt,
                               worker=worker, aggregate=aggregate, binary=binary, base=base, proof=proof)
    return make


def refresh(f, name=None):
    """Rebind only trusted outer hashes to test inner semantic checks."""
    if name:
        entry = f.worker["proofs"][name]
        directory = f.qualification / "worker" / name
        entry["sha256"] = release.sha256(directory / "report.json")
        entry["case_results"] = release._case_hashes(directory, name, common)
    put(f.qualification / "worker/qualification.json", f.worker)
    f.aggregate["worker_report_sha256"] = release.sha256(f.qualification / "worker/qualification.json")
    f.aggregate["proofs"] = {name: {**item, "report": "worker/" + item["report"]} for name, item in f.worker["proofs"].items()}
    put(f.qualification / "qualification.json", f.aggregate)


@pytest.mark.parametrize("target", release.TARGETS)
def test_complete_bound_qualification_and_exact_deterministic_installer_archive(fixture, target):
    from supervisor.runtime import native_codex_install as installer
    f = fixture(target)
    before = {name: release.sha256(f.candidate / name) for name in f.receipt["files"]}
    first = release.package(target, f.candidate, f.qualification, f.base / "package-one")
    second = release.package(target, f.candidate, f.qualification, f.base / "package-two")
    assert release.sha256(first) == release.sha256(second)
    checks, path = release.verify_artifact(target, first.parent)
    assert path == first and checks["files"][f.binary.relative_to(f.candidate).as_posix()] == release.sha256(f.binary)
    assert release.payload(target) == installer._bundle_files(release.profile(target)[0])
    with tarfile.open(first, "r:gz") as archive:
        assert set(archive.getnames()) == release.payload(target)
        assert not any(name in archive.getnames() for name in ("Cargo.lock", "native-build-receipt.json"))
        for member in archive:
            assert member.isfile() and member.mtime == member.uid == member.gid == 0
            assert member.mode == (0o755 if member.name in release.executables(target) else 0o644)
        info = json.load(archive.extractfile("BUILD-INFO"))
        assert info["release_provenance"]["build_receipt_sha256"] == release.sha256(f.candidate / "native-build-receipt.json")
        manifest = json.load(archive.extractfile("selection-manifest.json"))
        assert (manifest.get("transports") == ["tcp-hmac-v1"]) is (target == "windows-x64")
    assert before == {name: release.sha256(f.candidate / name) for name in before}


@pytest.mark.parametrize("field,value", [("passed", False), ("phase", "partial"), ("target", "other"),
    ("published", True), ("candidate_unchanged", False), ("modernbert_requested", False),
    ("paid_model_calls", 1), ("paid_model_calls", False), ("external_provider_requests_forwarded", 1),
    ("build_identity", {}), ("candidate_files", {}), ("proof_inputs", {}), ("build_receipt_sha256", "0" * 64),
    ("binary_sha256", "0" * 64), ("native_version", {"version": "0.155.1"})])
@pytest.mark.parametrize("layer", ["worker", "aggregate"])
def test_stale_incomplete_paid_or_wrong_identity_never_packages(fixture, field, value, layer):
    f = fixture()
    getattr(f, layer)[field] = value
    refresh(f)
    with pytest.raises(ValueError):
        release.package(f.target, f.candidate, f.qualification, f.base / "rejected")
    assert not (f.base / "rejected").exists()


@pytest.mark.parametrize("field,value", [("exit_code", 1), ("exit_code", False), ("fenced", False),
    ("scope", "groups"), ("stopped", True), ("owner_pid", True), ("owner_pid", 0)])
def test_guardian_tree_cleanup_strict_for_linux_windows(fixture, field, value):
    f = fixture()
    f.aggregate["guardian"]["receipts"][0][field] = value
    refresh(f)
    with pytest.raises(ValueError, match="Guardian"):
        release.validate_qualification(f.target, f.candidate, f.qualification)


@pytest.mark.parametrize("change", ["missing", "duplicate", "watchdog-failed", "worker-hash", "worker-path"])
def test_guardian_and_worker_binding_cannot_be_omitted(fixture, change):
    f = fixture()
    if change == "missing": f.aggregate["guardian"]["receipts"].clear()
    elif change == "duplicate": f.aggregate["guardian"]["receipts"] *= 2
    elif change == "watchdog-failed": f.aggregate["guardian"]["watchdog_exit_code"] = 1
    elif change == "worker-hash": f.aggregate["worker_report_sha256"] = "0" * 64
    else: f.aggregate["worker_report"] = "../qualification.json"
    put(f.qualification / "qualification.json", f.aggregate)
    with pytest.raises(ValueError): release.validate_qualification(f.target, f.candidate, f.qualification)


@pytest.mark.parametrize("change", ["missing-proof", "extra-proof", "wrong-count", "report-path", "report-bytes",
    "missing-case", "extra-case", "case-bytes", "case-semantic", "async-case-semantic", "paid-proof", "model-bypass", "not-cpu"])
def test_all_saved_case_hashes_counts_and_semantics_required(fixture, change):
    f = fixture()
    if change == "missing-proof": del f.worker["proofs"]["modernbert"]
    elif change == "extra-proof": f.worker["proofs"]["unknown"] = {"report": "unknown/report.json"}
    elif change == "wrong-count": f.worker["proofs"]["async"]["cases"] = 13
    elif change == "report-path": f.worker["proofs"]["selection"]["report"] = "../selection/report.json"
    elif change == "report-bytes":
        path = f.qualification / "worker/selection/report.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif change in {"missing-case", "extra-case", "case-bytes", "case-semantic", "async-case-semantic"}:
        path = f.qualification / "worker/selection/direct_on/result.json"
        if change == "missing-case": path.unlink()
        elif change == "extra-case": put(path.parent.parent / "unproven/result.json", case("unproven"))
        elif change == "case-bytes": path.write_bytes(path.read_bytes() + b" ")
        else:
            name = "async" if change == "async-case-semantic" else "selection"
            if name == "async": path = f.qualification / "worker/async/async_and_selection_direct/result.json"
            value = release.read_json(path)
            value["passed"] = False
            put(path, value)
            refresh(f, name)
    else:
        path = f.qualification / "worker/modernbert/report.json"
        value = release.read_json(path)
        if change == "paid-proof": value["paid_model_calls"] = 1
        elif change == "not-cpu": value["device"] = "cuda"
        else: value["cases"][0]["model_bypassed"] = False
        put(path, value)
        refresh(f, "modernbert")
    refresh(f)
    with pytest.raises(ValueError): release.validate_qualification(f.target, f.candidate, f.qualification)


@pytest.mark.parametrize("which", ["binary", "companion", "receipt", "extra", "hardlink", "symlink", "dirlink"])
def test_candidate_or_proof_topology_tampering_fails_closed(fixture, which):
    f = fixture()
    if which == "binary": f.binary.write_bytes(b"changed")
    elif which == "companion": (f.candidate / "bin/codex-code-mode-host").write_bytes(b"changed")
    elif which == "receipt": put(f.candidate / "native-build-receipt.json", {})
    elif which == "extra": (f.candidate / "surprise").write_text("extra")
    else:
        path = f.qualification / "worker/selection/direct_on/result.json"
        if which == "hardlink": os.link(path, f.base / "alias.json")
        elif which == "symlink":
            other = f.base / "alias.json"
            path.rename(other)
            try:
                path.symlink_to(other)
            except OSError:
                pytest.skip("Symlink creation requires native host permission")
        else:
            other = f.base / "alias-dir"
            path.parent.rename(other)
            try:
                path.parent.symlink_to(other, target_is_directory=True)
            except OSError:
                pytest.skip("Directory symlink creation requires native host permission")
    with pytest.raises(ValueError): release.validate_qualification(f.target, f.candidate, f.qualification)


def test_macos_never_claims_tree_recovery(fixture):
    f = fixture("darwin-arm64")
    assert release.validate_qualification(f.target, f.candidate, f.qualification)["guardian_scope"] == "groups"
    f.worker["full_tree_recovery_proven"] = True
    refresh(f)
    with pytest.raises(ValueError, match="disclaim"):
        release.validate_qualification(f.target, f.candidate, f.qualification)


def test_no_existing_nested_or_source_output(fixture):
    f = fixture()
    for path in (f.candidate, f.candidate / "output", f.qualification / "output", release.ROOT / "never-create"):
        with pytest.raises(ValueError): release.package(f.target, f.candidate, f.qualification, path)


@pytest.mark.parametrize("change", ["archive", "hash", "size", "target", "source", "identity"])
def test_artifact_checksum_and_provenance_checked_again(fixture, change):
    f = fixture()
    archive = release.package(f.target, f.candidate, f.qualification, f.base / "package")
    path = archive.parent / "checksums.json"
    checks = release.read_json(path)
    if change == "archive": archive.write_bytes(archive.read_bytes() + b"changed")
    elif change == "hash": checks["archive_sha256"] = "0" * 64
    elif change == "size": checks["archive_size"] += 1
    elif change == "target": checks["target"] = "windows-x64"
    elif change == "identity": checks["build_identity"] = {}
    else: checks["packaging_inputs"] = {}
    put(path, checks)
    with pytest.raises(ValueError): release.verify_artifact(f.target, archive.parent)


@pytest.mark.parametrize("body", [b'{"x":1,"x":2}', b'{"x":NaN}', b'[]'])
def test_strict_bounded_json(body):
    with pytest.raises(ValueError): release._decode(body)


def test_archive_limits_and_missing_files(fixture, monkeypatch):
    f = fixture()
    monkeypatch.setattr(release, "MAX_UNPACKED", 1)
    with pytest.raises(ValueError, match="size limit"):
        release.package(f.target, f.candidate, f.qualification, f.base / "too-large")


@pytest.mark.parametrize("change", ["traversal", "absolute", "extra", "duplicate", "symlink", "hardlink", "directory", "mode", "owner", "missing", "duplicate-json"])
def test_archive_entry_inventory_and_metadata_fail_closed_even_with_rebound_outer_digest(fixture, change):
    f = fixture()
    archive = release.package(f.target, f.candidate, f.qualification, f.base / "package")
    with tarfile.open(archive, "r:gz") as stream:
        entries = [(member, stream.extractfile(member).read()) for member in stream]
    first, content = entries[0]
    if change == "traversal": first.name = "../escape"
    elif change == "absolute": first.name = "/escape"
    elif change == "extra": first.name = "unexpected"
    elif change == "duplicate": entries.append((deepcopy(first), content))
    elif change == "symlink": first.type, first.linkname, first.size = tarfile.SYMTYPE, "outside", 0
    elif change == "hardlink": first.type, first.linkname, first.size = tarfile.LNKTYPE, "outside", 0
    elif change == "directory": first.type, first.size = tarfile.DIRTYPE, 0
    elif change == "mode": first.mode = 0o777
    elif change == "owner": first.uid = 42
    elif change == "missing": entries.pop()
    else:
        for index, (member, data) in enumerate(entries):
            if member.name == "selection-manifest.json":
                data = b'{"version":"0.161.0",' + data.lstrip()[1:]
                member.size = len(data)
                entries[index] = member, data
    with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT) as stream:
        for member, data in entries:
            stream.addfile(member, io.BytesIO(data) if member.isfile() else None)
    checks = release.read_json(archive.parent / "checksums.json")
    checks.update(archive_sha256=release.sha256(archive), archive_size=archive.stat().st_size)
    put(archive.parent / "checksums.json", checks)
    with pytest.raises(ValueError): release.verify_artifact(f.target, archive.parent)


def test_installer_code_identity_excludes_only_pin_values(tmp_path, monkeypatch):
    path = tmp_path / "supervisor/runtime/native_codex_install.py"
    path.parent.mkdir(parents=True)
    original = (release.ROOT / "supervisor/runtime/native_codex_install.py").read_bytes()
    monkeypatch.setattr(release, "ROOT", tmp_path)
    path.write_bytes(original)
    before = release.installer_implementation()
    starts = [0]
    for line in original.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    replacements = []
    for node in release.ast.parse(original.decode("utf-8")).body:
        if (isinstance(node, release.ast.AnnAssign)
                and isinstance(node.target, release.ast.Name)
                and node.target.id in {"BUNDLES", "ASYNC_BUNDLES"}):
            for call in node.value.values:
                literals = call.args or [keyword.value for keyword in call.keywords]
                for literal in literals:
                    start = starts[literal.lineno - 1] + literal.col_offset
                    end = starts[literal.end_lineno - 1] + literal.end_col_offset
                    # Deliberately synthetic strings, never an executable/download fixture.
                    replacement = repr(literal.value + "-pin-mask-regression").encode("utf-8")
                    assert replacement != original[start:end]
                    replacements.append((start, end, replacement))
    assert len(replacements) == 18
    changed = original
    for start, end, replacement in sorted(replacements, reverse=True):
        changed = changed[:start] + replacement + changed[end:]
    assert changed != original
    assert hashlib.sha256(changed).digest() != hashlib.sha256(original).digest()
    path.write_bytes(changed)
    assert release.installer_implementation() == before
    path.write_bytes(original.replace(b"_MAX_DOWNLOAD = 1024 * 1024 * 1024", b"_MAX_DOWNLOAD = 1"))
    assert release.installer_implementation() != before
    path.write_bytes(original.replace(b"ASYNC_BUNDLES:", b"RENAMED_BUNDLES:"))
    with pytest.raises(ValueError): release.installer_implementation()


def test_installer_normalization_uses_utf8_spans_not_python_ast_schema(tmp_path, monkeypatch):
    path = tmp_path / "supervisor/runtime/native_codex_install.py"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(release, "ROOT", tmp_path)
    table = '{("Linux", "x86_64"): NativeBundle("first", "second", "third"), ("Darwin", "arm64"): NativeBundle("first", "second", "third"), ("Windows", "x86_64"): NativeBundle("first", "second", "third")}'
    source = f'# café\nlabel = "日本語"; BUNDLES: dict = {table}\nASYNC_BUNDLES: dict = {table}\ndef method():\n    return 1\n'
    path.write_text(source, encoding="utf-8", newline="\n")
    expected = source.replace('"first"', "'__BELLO_RELEASE_PIN_LITERAL__'").replace('"second"', "'__BELLO_RELEASE_PIN_LITERAL__'").replace('"third"', "'__BELLO_RELEASE_PIN_LITERAL__'")
    before = release.installer_implementation()
    assert before == hashlib.sha256(expected.encode()).hexdigest()
    original = release.ast.parse
    def new_python_schema(*args, **kwargs):
        tree = original(*args, **kwargs)
        for node in release.ast.walk(tree):
            if isinstance(node, release.ast.FunctionDef):
                node.type_params = []
                node._fields = (*node._fields, "future_python_field")
                node.future_python_field = "new AST schema"
        return tree
    monkeypatch.setattr(release.ast, "parse", new_python_schema)
    assert release.installer_implementation() == before
    path.write_text(source.replace('"first"', '"another release"'), encoding="utf-8", newline="\n")
    assert release.installer_implementation() == before
    path.write_text(source.replace('# café', '# changed comment'), encoding="utf-8", newline="\n")
    assert release.installer_implementation() != before


@pytest.mark.parametrize("mutation", ["expression", "effectful", "constructor", "duplicate-key", "dynamic-key", "unpack", "star", "duplicate-field"])
def test_pin_normalization_never_hides_executable_semantics(tmp_path, monkeypatch, mutation):
    path = tmp_path / "supervisor/runtime/native_codex_install.py"
    path.parent.mkdir(parents=True)
    table = '''{
    ("Linux", "x86_64"): NativeBundle(url="url-linux", archive_sha256="archive-linux", manifest_sha256="manifest-linux"),
    ("Darwin", "arm64"): NativeBundle(url="url-mac", archive_sha256="archive-mac", manifest_sha256="manifest-mac"),
    ("Windows", "x86_64"): NativeBundle(url="url-windows", archive_sha256="archive-windows", manifest_sha256="manifest-windows"),
}'''
    original = f"BUNDLES: dict = {table}\nASYNC_BUNDLES: dict = {table}\n"
    monkeypatch.setattr(release, "ROOT", tmp_path)
    if mutation == "expression": source = original.replace('url="url-linux"', 'url=str("url-linux")', 1)
    elif mutation == "effectful": source = original.replace('archive_sha256="archive-linux"', 'archive_sha256=(effect() or "archive-linux")', 1)
    elif mutation == "constructor": source = original.replace('NativeBundle(', 'OtherBundle(', 1)
    elif mutation == "duplicate-key": source = original.replace('("Darwin", "arm64"):', '("Linux", "x86_64"):', 1)
    elif mutation == "dynamic-key": source = original.replace('("Linux", "x86_64"):', '(get_system(), "x86_64"):', 1)
    elif mutation == "unpack": source = original.replace('BUNDLES: dict = {', 'BUNDLES: dict = {**other,', 1)
    elif mutation == "star": source = original.replace('NativeBundle(', 'NativeBundle(*other,', 1)
    else: source = original.replace('manifest_sha256="manifest-linux"', 'archive_sha256="manifest-linux"', 1)
    assert source != original
    path.write_text(source, newline="\n")
    with pytest.raises((ValueError, SyntaxError)):
        release.installer_implementation()


def test_packaging_cli_imports_before_optional_host_dependencies():
    result = subprocess.run([sys.executable, "-S", str(Path(release.__file__)), "--help"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and "install-published" in result.stdout


def test_packaging_identity_binds_direct_lock_and_filesystem_security_dependencies():
    inputs = release.packaging_inputs("linux-x64")
    for name in ("supervisor/state.py", "supervisor/filesystem_safety.py", "supervisor/runtime/native_codex_layout.py"):
        assert inputs[name] == release.sha256(release.ROOT / name)


@pytest.mark.parametrize("target", release.TARGETS)
def test_public_proof_rejects_old_or_different_capability_pins(monkeypatch, target):
    from supervisor.runtime import native_codex_install as installer
    key = release.profile(target)[:2]
    old = installer.NativeBundle("https://github.com/Makson179/Bello/releases/download/old/bello-native-codex-0.155.1.tar.gz", "a" * 64, "b" * 64)
    monkeypatch.setattr(installer, "BUNDLES", {key: old})
    monkeypatch.setattr(installer, "ASYNC_BUNDLES", {key: old})
    with pytest.raises(ValueError): release.published_bundle(target)
    spec = installer.NativeBundle("https://github.com/Makson179/Bello/releases/download/native-0161/"
        f"bello-native-codex-0.161.0-{release.profile(target)[2]}.tar.gz", "a" * 64, "b" * 64)
    monkeypatch.setattr(installer, "BUNDLES", {key: spec})
    monkeypatch.setattr(installer, "ASYNC_BUNDLES", {key: spec})
    assert release.published_bundle(target)["archive_sha256"] == "a" * 64
    saved = installer._download
    with release._distribution(installer, target, release.published_bundle(target), None):
        assert installer._download is saved
    installer.ASYNC_BUNDLES.clear()
    with pytest.raises(ValueError): release.published_bundle(target)


def test_local_transfer_injection_preserves_all_installer_logic(fixture, tmp_path):
    from supervisor.runtime import native_codex_install as installer
    f = fixture()
    archive = release.package(f.target, f.candidate, f.qualification, f.base / "package")
    checks = release.read_json(archive.parent / "checksums.json")
    previous = installer._download, dict(installer.BUNDLES), dict(installer.ASYNC_BUNDLES)
    with release.local_bundle(installer, f.target, checks, archive) as (spec, transfers):
        installer._download(spec, tmp_path / "copied.tar.gz")
        assert transfers == [True]
        with pytest.raises(ValueError): installer._download(spec, tmp_path / "second.tar.gz")
        assert installer.BUNDLES == installer.ASYNC_BUNDLES == {("Linux", "x86_64"): spec}
    assert (installer._download, installer.BUNDLES, installer.ASYNC_BUNDLES) == previous


def test_install_worker_requires_authentication_before_outputs(fixture, monkeypatch):
    from supervisor import process_fence
    f = fixture()
    monkeypatch.setattr(process_fence, "configure_worker", lambda: None)
    monkeypatch.setattr(process_fence, "is_guarded_worker", lambda: False)
    with pytest.raises(RuntimeError, match="authenticated"):
        asyncio.run(release._install_worker(f.target, f.candidate, f.base / "runtime", f.base / "proof"))
    assert not (f.base / "runtime").exists() and not (f.base / "proof").exists()


@pytest.mark.parametrize("published", [False, True])
def test_installed_real_unpack_cache_and_proof_orchestration(fixture, monkeypatch, published):
    """Real installer extraction/security; only native execution is simulated."""
    from supervisor import process_fence
    from supervisor.runtime import native_codex_install as installer, codex_distiller
    f = fixture("darwin-arm64")
    archive = release.package(f.target, f.candidate, f.qualification, f.base / "package")
    monkeypatch.setattr(release, "require_platform", lambda _: None)
    monkeypatch.setattr(installer, "_platform_key", lambda: ("Darwin", "arm64"))
    monkeypatch.setattr(process_fence, "configure_worker", lambda: None)
    monkeypatch.setattr(process_fence, "is_guarded_worker", lambda: True)
    async def capability(*_): return {}
    async def run(name, binary, output):
        assert "OPENAI_API_KEY" not in os.environ and "HF_TOKEN" not in os.environ
        return write_proof(name, binary, output)
    monkeypatch.setattr(codex_distiller, "validate_native_selection", capability)
    monkeypatch.setattr(common, "run_proof", run)
    monkeypatch.setattr(common, "native_version", lambda binary: {"version": release.VERSION, "exit_code": 0, "binary_sha256": release.sha256(binary)})
    monkeypatch.setenv("OPENAI_API_KEY", "unit-secret-must-not-leak")
    monkeypatch.setenv("HF_TOKEN", "unit-secret-must-not-leak")
    environment = dict(os.environ)
    downloads = []
    if published:
        checks = release.read_json(archive.parent / "checksums.json")
        spec = installer.NativeBundle("https://github.com/Makson179/Bello/releases/download/unit-only/" + archive.name,
                                     checks["archive_sha256"], checks["manifest_sha256"])
        monkeypatch.setattr(installer, "BUNDLES", {("Darwin", "arm64"): spec})
        monkeypatch.setattr(installer, "ASYNC_BUNDLES", {("Darwin", "arm64"): spec})
        def mock_production_transfer(bundle, destination):
            assert bundle == spec
            downloads.append(True)
            destination.write_bytes(archive.read_bytes())
        monkeypatch.setattr(installer, "_download", mock_production_transfer)
    report = asyncio.run(release._install_worker(f.target, None if published else archive.parent, f.base / "runtime", f.base / "installed"))
    assert report["passed"] is True and report["cache_unchanged"] is True
    assert report["local_archive_transfers"] == (0 if published else 1) and report["private_cache_validation"] is True
    assert report["published"] is published
    assert report["distribution_source"] == ("production-https" if published else "local-archive")
    if published:
        assert downloads == [True] and installer._download is mock_production_transfer
    assert set(report["proofs"]) == {"selection", "async", "history", "modernbert"}
    assert os.environ == environment and "unit-secret" not in json.dumps(report)
    assert Path(report["installed_directory"]).name == release.sha256(archive)


@pytest.mark.parametrize("failure", [None, "fenced", "scope", "stopped", "exit", "missing", "error", "case-tamper", "cache-tamper", "version", "version-exit", "version-hash"])
def test_installed_worker_success_never_hides_guardian_or_postexecution_failure(fixture, monkeypatch, failure):
    from supervisor import process_fence, watchdog
    from supervisor.runtime import native_codex_install as installer, codex_distiller
    f = fixture("darwin-arm64")
    archive = release.package(f.target, f.candidate, f.qualification, f.base / "package")
    monkeypatch.setattr(release, "require_platform", lambda _: None)
    monkeypatch.setattr(installer, "_platform_key", lambda: ("Darwin", "arm64"))
    monkeypatch.setattr(process_fence, "configure_worker", lambda: None)
    monkeypatch.setattr(process_fence, "is_guarded_worker", lambda: True)
    async def capability(*_): return {}
    async def run(name, binary, output): return write_proof(name, binary, output)
    monkeypatch.setattr(codex_distiller, "validate_native_selection", capability)
    monkeypatch.setattr(common, "run_proof", run)
    monkeypatch.setattr(common, "native_version", lambda binary: {"version": release.VERSION, "exit_code": 0, "binary_sha256": release.sha256(binary)})
    def guarded(command, env, *, cwd):
        if failure == "error": raise RuntimeError("unit-secret-must-not-leak")
        args = {flag: Path(command[command.index(flag) + 1]) for flag in ("--artifact", "--runtime-root", "--output")}
        child = asyncio.run(release._install_worker(f.target, args["--artifact"], args["--runtime-root"], args["--output"]))
        assert child["passed"]
        if failure == "case-tamper":
            path = args["--output"] / "selection/direct_on/result.json"
            path.write_bytes(path.read_bytes() + b" ")
        if failure == "cache-tamper":
            (Path(child["installed_directory"]) / "bin/codex").write_bytes(b"modified")
        if failure in {"version", "version-exit", "version-hash"}:
            field, value = {"version": ("version", "0.155.1"), "version-exit": ("exit_code", 1), "version-hash": ("binary_sha256", "0" * 64)}[failure]
            child["native_version"][field] = value
            put(args["--output"] / "report.json", child)
        row = {"exit_code": 0, "fenced": True, "scope": "groups", "stopped": False, "owner_pid": 123}
        if failure == "fenced": row["fenced"] = False
        if failure == "scope": row["scope"] = "none"
        if failure == "stopped": row["stopped"] = True
        if failure == "exit": row["exit_code"] = 1
        return row
    monkeypatch.setattr(watchdog, "_run_guarded", guarded)
    def watch(command, **kwargs):
        assert kwargs["maximum_restarts"] == 0 and kwargs["required_scope"] == "groups"
        assert kwargs["disposition"](None)["eligible"] is False
        if failure != "missing": watchdog._run_guarded(command, dict(os.environ), cwd=kwargs["project_root"])
        return 0
    monkeypatch.setattr(watchdog, "watch_command", watch)
    report = release.install_local(f.target, archive.parent, f.base / "runtime", f.base / "installed", modernbert=True)
    assert report["passed"] is (failure is None)
    assert watchdog._run_guarded is guarded and "unit-secret" not in json.dumps(report)
    if report["passed"]:
        assert report["guardian_scope"] == "groups" and any("full process-tree" in item for item in report["not_covered"])


def test_installed_proof_requires_real_ml_and_platform_before_spawning(fixture, monkeypatch):
    f = fixture()
    monkeypatch.setattr(release, "require_platform", lambda _: None)
    with pytest.raises(ValueError, match="modernbert"):
        release.install_local(f.target, f.candidate, f.base / "runtime", f.base / "proof", modernbert=False)
