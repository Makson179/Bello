"""Public Rust runtimes stay scoped to one discovered version."""

from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from supervisor.runtime import sandbox


class VirtualPosixPath(PurePosixPath):
    """Model the Linux runtime inventory independently of the test host."""

    def exists(self):
        return self == RUST

    def resolve(self):
        return self

    def is_file(self):
        return False


RUST = VirtualPosixPath("/usr/local/rustup/toolchains/1.92.0-x86_64-unknown-linux-gnu")


@pytest.fixture
def public_rust(monkeypatch):
    monkeypatch.setattr(sandbox, "Path", VirtualPosixPath)
    monkeypatch.setattr(sandbox, "sys", SimpleNamespace(platform="linux"))


@pytest.mark.parametrize("tool", ["rustc", "cargo"])
def test_public_rust_exposes_only_selected_version(public_rust, tool):
    assert sandbox._tool_runtime_root(RUST / "bin" / tool) == RUST


@pytest.mark.parametrize("value", [
    "/usr/local/rustup/settings.toml",
    "/usr/local/rustup/toolchains",
    "/usr/local/cargo/bin/rustup",
    "/tmp/usr/local/rustup/toolchains/other/bin/rustc",
    "/controller-home/rustup/toolchains/other/bin/rustc",
    "/usr/local/rustup-other/toolchains/other/bin/rustc",
])
def test_public_rust_does_not_grant_parent_or_unrelated_roots(public_rust, value):
    path = VirtualPosixPath(value)
    assert sandbox._tool_runtime_root(path) == path
