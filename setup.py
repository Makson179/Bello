"""Setuptools hooks for Bello's platform-specific Windows sandbox helper."""

from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


_ROOT = Path(__file__).resolve().parent
_NATIVE = _ROOT / "native" / "windows-sandbox"
_TARGET = _NATIVE / "target"
_HELPER = "bello-windows-sandbox.exe"
_NOTICE = "THIRD_PARTY_NOTICES.txt"


def _require_windows_amd64() -> None:
    machine = platform.machine().lower()
    if machine not in {"amd64", "x86_64"} or struct.calcsize("P") != 8:
        raise RuntimeError(
            "Bello restricted execution currently supports only 64-bit x86 Windows; "
            f"got machine={platform.machine()!r}, pointer_bits={struct.calcsize('P') * 8}"
        )


def _build_windows_helper(build_lib: str) -> None:
    _require_windows_amd64()
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError(
            "building Bello on Windows requires Rust/Cargo so the mandatory native "
            "sandbox helper can be built; refusing to produce an incomplete wheel"
        )
    manifest = _NATIVE / "Cargo.toml"
    lock = _NATIVE / "Cargo.lock"
    notice = _NATIVE / _NOTICE
    for required in (manifest, lock, notice):
        if not required.is_file():
            raise RuntimeError(f"required Windows sandbox build input is missing: {required}")
    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = os.fspath(_TARGET)
    existing_flags = environment.get("RUSTFLAGS", "").strip()
    environment["RUSTFLAGS"] = " ".join(
        part for part in (existing_flags, "-C target-feature=+crt-static") if part
    )
    subprocess.run(
        [
            cargo,
            "build",
            "--locked",
            "--release",
            "--target",
            "x86_64-pc-windows-msvc",
            "--manifest-path",
            os.fspath(manifest),
        ],
        cwd=_ROOT,
        env=environment,
        check=True,
    )
    binary = _TARGET / "x86_64-pc-windows-msvc" / "release" / _HELPER
    if not binary.is_file() or binary.stat().st_size == 0:
        raise RuntimeError(f"Cargo did not produce the required Windows sandbox helper: {binary}")
    destination = Path(build_lib) / "supervisor" / "runtime" / "bin"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, destination / _HELPER)
    shutil.copy2(notice, destination / _NOTICE)


class BuildPy(_build_py):
    def run(self) -> None:
        super().run()
        if os.name == "nt":
            _build_windows_helper(self.build_lib)


cmdclass: dict[str, type] = {"build_py": BuildPy}
if os.name == "nt":
    from wheel.bdist_wheel import bdist_wheel as _bdist_wheel

    class WindowsBdistWheel(_bdist_wheel):
        def finalize_options(self) -> None:
            super().finalize_options()
            self.root_is_pure = False

        def get_tag(self) -> tuple[str, str, str]:
            _require_windows_amd64()
            return "py3", "none", "win_amd64"

    cmdclass["bdist_wheel"] = WindowsBdistWheel


setup(cmdclass=cmdclass)
