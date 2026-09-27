"""Offline public-header mount regression: no native processes or external IO."""
from pathlib import Path

import pytest

from supervisor.runtime import sandbox


HEADERS = Path("/usr/include")


def virtual_headers(monkeypatch, *, exists=True, directory=True, symlink=False, resolved=HEADERS):
    original_exists = Path.exists
    original_is_dir = Path.is_dir
    original_is_symlink = Path.is_symlink
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "exists", lambda p: exists if p == HEADERS else original_exists(p))
    monkeypatch.setattr(Path, "is_dir", lambda p: directory if p == HEADERS else original_is_dir(p))
    monkeypatch.setattr(Path, "is_symlink", lambda p: symlink if p == HEADERS else original_is_symlink(p))
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **kw: resolved if p == HEADERS else original_resolve(p, *a, **kw))
    monkeypatch.setattr(sandbox, "_runtime_root", lambda: None)


def pairs(argv, option):
    return {(argv[i + 1], argv[i + 2]) for i, part in enumerate(argv) if part == option}


def test_ordinary_public_headers_are_in_actual_system_mount_inventory(monkeypatch):
    virtual_headers(monkeypatch)
    assert (HEADERS, HEADERS) in sandbox._linux_system_mounts()


@pytest.mark.parametrize("fault", ["missing", "file", "symlink", "symlinked-parent"])
def test_header_mount_rejects_missing_non_directory_and_symlink_authorities(monkeypatch, fault):
    virtual_headers(monkeypatch, exists=fault != "missing", directory=fault != "file",
                    symlink=fault == "symlink",
                    resolved=Path("/controller-home/private") if fault == "symlinked-parent" else HEADERS)
    mounts = sandbox._linux_system_mounts()
    assert not any(destination == HEADERS for _, destination in mounts)
    assert not any(source == Path("/controller-home/private") for source, _ in mounts)


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
@pytest.mark.parametrize("network", [False, True])
def test_header_addition_is_readonly_and_preserves_private_namespace_contract(monkeypatch, tmp_path, mode, network):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".supervisor").mkdir()
    (root / ".supervisor/secret").write_text("synthetic private controller value")
    virtual_headers(monkeypatch)
    monkeypatch.setattr(sandbox, "_linux_launcher", lambda: Path("/usr/bin/bwrap"))
    policy = sandbox.SandboxPolicy(root, mode=mode, network_access=network)
    invocation = sandbox._linux_invocation(policy, root, ("/usr/bin/true",))
    readonly = pairs(invocation.argv, "--ro-bind")
    writable = pairs(invocation.argv, "--bind")
    assert ("/usr/include", "/usr/include") in readonly
    assert ("/usr/include", "/usr/include") not in writable
    assert ("/usr", "/usr") not in readonly | writable
    for private in ("/provider-auth", "/controller-home", "/state", "/opt/bello-sonnet", "/run", "/var/run/docker.sock", "/home"):
        assert not any(source == private or source.startswith(private + "/") for source, _ in readonly | writable)
    assert ("dir", (root / ".supervisor").resolve()) in sandbox._linux_masks(policy)
    assert str(root / ".supervisor") in invocation.argv
    for option in ("--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--cap-drop", "--clearenv", "--proc"):
        assert option in invocation.argv
    assert ("--unshare-net" in invocation.argv) is not network
    assert "--share-net" not in invocation.argv
    assert not any(value.endswith("-try") for value in invocation.argv)
    assert set(invocation.env) == {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"}


def test_private_namespace_symlink_into_new_header_authority_still_fails_closed(monkeypatch, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".supervisor").symlink_to(HEADERS, target_is_directory=True)
    virtual_headers(monkeypatch)
    monkeypatch.setattr(sandbox, "_linux_launcher", lambda: Path("/usr/bin/bwrap"))
    with pytest.raises(sandbox.SandboxPolicyError, match="private sandbox namespaces cannot be symbolic links"):
        sandbox._linux_invocation(sandbox.SandboxPolicy(root), root, ("/usr/bin/true",))
