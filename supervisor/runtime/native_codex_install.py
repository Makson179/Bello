"""Pinned, private native Codex helper for log selection, not a global CLI update.

Only the native-Codex + distiller path calls this module. Downloads happen before
starting app-server and outside its RPC deadlines. Explicit host overrides remain
supported and go through the existing native capability validation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import tarfile
import tempfile
from urllib.request import Request, urlopen

from supervisor.filesystem_safety import is_link_or_reparse
from supervisor.state import FileLock


@dataclass(frozen=True)
class NativeBundle:
    url: str
    archive_sha256: str
    manifest_sha256: str


# Add a platform only after the offline native provider-boundary proof passes.
BUNDLES: dict[tuple[str, str], NativeBundle] = {
    ("Darwin", "arm64"): NativeBundle(
        url=("https://github.com/Makson179/Bello/releases/download/"
             "native-codex-0.153.4-selection-v1/"
             "bello-native-codex-0.153.4-aarch64-apple-darwin.tar.gz"),
        archive_sha256="587bdeb27a9d6896252238805b2057e15b6d196cbec1411f76d5e2563e81ced2",
        manifest_sha256="cc16402a699eba247b6fe34fd7f71328d318f8d5a83ef836498d8dbadd0e136e",
    ),
    ("Windows", "x86_64"): NativeBundle(
        url=("https://github.com/Makson179/Bello/releases/download/"
             "native-codex-0.153.4-selection-v1/"
             "bello-native-codex-0.153.4-x86_64-pc-windows-msvc.tar.gz"),
        archive_sha256="dc9628bda906e259b2838e801ebd12a061b3f6949362102c3d556eded4768d4e",
        manifest_sha256="533450f5c62f89bda3fa228089184e08709f0172b324d44543a14a0c73036723",
    ),
}
_MAX_DOWNLOAD = 1024 * 1024 * 1024
_MAX_UNPACKED = 2 * _MAX_DOWNLOAD
_IS_WINDOWS = os.name == "nt"
_FILES = frozenset({
    "bin/codex", "bin/codex-code-mode-host", "selection-manifest.json",
    "LICENSE", "NOTICE", "native-codex-selection.patch",
    "THIRD-PARTY-NOTICES", "BUILD-INFO",
})
_WINDOWS_FILES = (_FILES - {"bin/codex", "bin/codex-code-mode-host"}) | frozenset({
    "bin/codex.exe", "bin/codex-code-mode-host.exe", "bin/codex-command-runner.exe",
    "bin/codex-windows-sandbox-setup.exe",
})
_LINUX_FILES = _FILES | frozenset({"bin/codex-resources/bwrap"})


def _bundle_files(system: str) -> frozenset[str]:
    if system == "Windows":
        return _WINDOWS_FILES
    return _LINUX_FILES if system == "Linux" else _FILES


def _bundle_directories(system: str) -> frozenset[str]:
    return frozenset(str(parent) for name in _bundle_files(system)
                     for parent in PurePosixPath(name).parents)


def _windows_parent_readonly_rights(rights: str) -> bool:
    # FILE_GENERIC_READ | FILE_GENERIC_EXECUTE, plus their generic equivalents.
    # No WRITE_*, DELETE, FILE_DELETE_CHILD, WRITE_DAC or WRITE_OWNER bits.
    allowed = 0xA01200A9
    if re.fullmatch(r"0x[0-9a-fA-F]+", rights):
        return not int(rights, 16) & ~allowed
    # SDDL can render individual file bits using the shared two-letter aliases.
    return bool(re.fullmatch(r"(?:FR|FX|GR|GX|RC|SY|CC|SW|WP|LO)+", rights))


def _validate_windows_security_descriptor(descriptor: str, user_sid: str, *, parent: bool = False,
                                          user_alias: str | None = None,
                                          verified_public_executable: bool = False) -> None:
    """Fail closed on any grant outside the user, SYSTEM and administrators.

    These are private executable caches, not shared installation directories.
    Parsing the OS-produced SDDL permits only simple allow ACEs; unfamiliar ACLs
    are rejected rather than interpreted optimistically. Existing ACLs are never
    repaired. Administrators/SYSTEM remain trusted as on a normal user profile.
    A containing directory may allow others to read/traverse it. Its inherit-only
    ACEs do not apply to that directory, and the new cache has a protected DACL.
    Only a checksum-verified public launcher may retain simple read/execute ACEs
    added by native sandbox setup. No write, delete or ACL-changing grant is allowed.
    """
    owner, separator, dacl = descriptor.partition("D:")
    trusted = {user_sid, "SY", "BA", "S-1-5-18", "S-1-5-32-544"}
    if user_alias is not None:
        # Supplied only by the OS round-trip of this process token's user SID.
        # Do not assume aliases such as LA belong to an arbitrary current user.
        if user_alias != user_sid and not re.fullmatch(r"[A-Z]{2}", user_alias):
            raise ValueError("Invalid Windows current-user owner alias")
        trusted.add(user_alias)
    if not separator or owner.removeprefix("O:") not in trusted or not owner.startswith("O:"):
        raise ValueError("Native Codex cache must have a trusted Windows owner and private DACL")
    # Windows/Python may express the trusted owner's grant as OWNER RIGHTS.
    # This trustee is safe only after validating the actual owner above; OW is
    # not itself an acceptable owner identity.
    trusted_grants = trusted | {"OW", "S-1-3-4"}
    flags, _, entries = dacl.partition("(")
    if not re.fullmatch(r"(?:P|AI|AR)*", flags) or not entries:
        raise ValueError("Native Codex cache must have a private Windows DACL")
    aces = re.findall(r"\([^()]*\)", "(" + entries)
    if "".join(aces) != "(" + entries:
        raise ValueError("Native Codex cache has an unsupported Windows DACL")
    for ace in aces:
        fields = ace[1:-1].split(";")
        if (len(fields) != 6 or fields[0] != "A" or fields[3] or fields[4] or not fields[2]
                or not re.fullmatch(r"(?:OI|CI|NP|IO|ID)*", fields[1])):
            raise ValueError("Native Codex cache must not grant access to other Windows accounts")
        ace_flags = {fields[1][index:index + 2] for index in range(0, len(fields[1]), 2)}
        if fields[5] in trusted_grants or parent and "IO" in ace_flags:
            continue
        if (parent or verified_public_executable) and _windows_parent_readonly_rights(fields[2]):
            continue
        raise ValueError("Native Codex cache must not grant access to other Windows accounts")


def _windows_private_acl(path: Path, *, create: bool = False, parent: bool = False,
                         verified_public_executable: bool = False) -> None:
    """Atomically create a private directory, or validate an existing entry.

    chmod(0700) does not establish a Windows DACL on supported Python 3.11.
    Use OS APIs, without shell commands, account-name localization or pywin32.
    """
    if verified_public_executable and (create or parent or path.name != "codex.exe"):
        raise ValueError("Public read/execute ACL allowance is only for the verified codex.exe file")
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    functions = (
        (kernel, "GetCurrentProcess", [], wintypes.HANDLE),
        (kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        (kernel, "LocalFree", [pointer], pointer),
        (kernel, "CreateDirectoryW", [wintypes.LPCWSTR, pointer], wintypes.BOOL),
        (advapi, "OpenProcessToken", [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
        (advapi, "GetTokenInformation", [wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        (advapi, "ConvertSidToStringSidW", [pointer, ctypes.POINTER(pointer)], wintypes.BOOL),
        (advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), pointer], wintypes.BOOL),
        (advapi, "GetNamedSecurityInfoW", [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, pointer, pointer, pointer, pointer, ctypes.POINTER(pointer)], wintypes.DWORD),
        (advapi, "ConvertSecurityDescriptorToStringSecurityDescriptorW", [pointer, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer), pointer], wintypes.BOOL),
    )
    for library, name, arguments, result in functions:
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", pointer), ("inherit", wintypes.BOOL)]

    token = wintypes.HANDLE()
    allocated = []
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        length = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not length.value:
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, user, length, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_string = pointer()
        if not advapi.ConvertSidToStringSidW(pointer.from_buffer(user), ctypes.byref(sid_string)):
            raise ctypes.WinError(ctypes.get_last_error())
        allocated.append(sid_string)
        user_sid = ctypes.wstring_at(sid_string)
        # Windows can render this SID as LA (local Administrator), or another
        # SDDL alias. Derive its exact spelling from the OS rather than adding
        # account aliases to a global allowlist.
        owner_descriptor = pointer()
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                f"O:{user_sid}", 1, ctypes.byref(owner_descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        allocated.append(owner_descriptor)
        owner_sddl = pointer()
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                owner_descriptor, 1, 0x00000001, ctypes.byref(owner_sddl), None):
            raise ctypes.WinError(ctypes.get_last_error())
        allocated.append(owner_sddl)
        owner_text = ctypes.wstring_at(owner_sddl)
        if not owner_text.startswith("O:"):
            raise ValueError("Windows did not return the current user's owner identity")
        user_alias = owner_text[2:]
        if create:
            descriptor = pointer()
            sddl = f"O:{user_sid}D:P(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
            if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
                raise ctypes.WinError(ctypes.get_last_error())
            allocated.append(descriptor)
            security = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            if not kernel.CreateDirectoryW(str(path), ctypes.byref(security)):
                error = ctypes.get_last_error()
                if error != 183:  # ERROR_ALREADY_EXISTS: inspect, never chmod/repair.
                    raise ctypes.WinError(error)
        metadata = path.lstat()
        if is_link_or_reparse(path, stat_result=metadata):
            raise ValueError("Native Codex cache must not use Windows reparse points")
        if verified_public_executable and not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Public read/execute ACL allowance is only for the verified codex.exe file")
        descriptor = pointer()
        error = advapi.GetNamedSecurityInfoW(str(path), 1, 0x00000005, None, None, None, None,
                                           ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        allocated.append(descriptor)
        sddl = pointer()
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 0x00000005,
                                                                          ctypes.byref(sddl), None):
            raise ctypes.WinError(ctypes.get_last_error())
        allocated.append(sddl)
        _validate_windows_security_descriptor(ctypes.wstring_at(sddl), user_sid,
                                               parent=parent, user_alias=user_alias,
                                               verified_public_executable=verified_public_executable)
    finally:
        for allocation in reversed(allocated):
            kernel.LocalFree(allocation)
        if token:
            kernel.CloseHandle(token)


def _reject_windows_reparse_ancestors(path: Path) -> None:
    if _IS_WINDOWS:
        for ancestor in (path, *path.parents):
            if is_link_or_reparse(ancestor):
                raise ValueError("Native Codex cache must not traverse Windows reparse points")


def _private_directory(path: Path, *, parents: bool = False) -> None:
    _reject_windows_reparse_ancestors(path)
    if _IS_WINDOWS:
        if parents:
            try:
                path.parent.lstat()
            except FileNotFoundError:
                if path.parent == path:
                    raise ValueError("Native Codex cache requires an existing filesystem anchor")
                # Never create intermediate cache directories with inherited
                # public ACLs. Stop at the first existing ancestor, validate it
                # as a parent, and atomically create each missing private child.
                _private_directory(path.parent, parents=True)
        _reject_windows_reparse_ancestors(path)
        # A public parent could replace a private child using DELETE_CHILD.
        _windows_private_acl(path.parent, parent=True)
        _windows_private_acl(path, create=True)
    else:
        path.mkdir(parents=parents, exist_ok=True, mode=0o700)
    _owned(path, directory=True, private=True)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _regular(path: Path) -> bool:
    return path.is_file() and not is_link_or_reparse(path)


def _owned(path: Path, *, directory: bool = False, private: bool = False,
           verified_public_executable: bool = False) -> None:
    metadata = path.lstat()
    valid_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if is_link_or_reparse(path, stat_result=metadata) or not valid_type:
        raise ValueError(f"Native Codex cache path must be a real {'directory' if directory else 'file'}: {path}")
    if _IS_WINDOWS:
        if verified_public_executable:
            _windows_private_acl(path, verified_public_executable=True)
        else:
            _windows_private_acl(path)
    elif (metadata.st_uid != os.getuid()
          or stat.S_IMODE(metadata.st_mode) & (0o077 if private else 0o022)):
        raise ValueError(f"Native Codex cache path must be owned by this user and not writable by others: {path}")


def _verify(directory: Path, bundle: NativeBundle, *, system: str = "Darwin") -> None:
    expected_files = _bundle_files(system)
    executable = "bin/codex.exe" if system == "Windows" else "bin/codex"
    _owned(directory, directory=True, private=True)
    expected_directories = _bundle_directories(system)
    for relative in sorted(expected_directories, key=lambda value: (len(PurePosixPath(value).parts), value)):
        current = directory / relative
        _owned(current, directory=True, private=True)
        expected_children = {PurePosixPath(name).name for name in expected_files | expected_directories
                             if name != "." and str(PurePosixPath(name).parent) == relative}
        if {path.name for path in current.iterdir()} != expected_children:
            raise ValueError("Native Codex cache contains unexpected bundled files")
    manifest_path = directory / "selection-manifest.json"
    if not _regular(manifest_path) or manifest_path.stat().st_size > 64 * 1024:
        raise ValueError("Native Codex capability manifest is missing or invalid")
    _owned(manifest_path)
    if _sha256(manifest_path) != bundle.manifest_sha256:
        raise ValueError("Native Codex capability manifest checksum mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or set(files) != expected_files - {"selection-manifest.json"}:
        raise ValueError("Native Codex bundle file manifest is incomplete")
    for name, digest in files.items():
        path = directory / name
        if not _regular(path) or _sha256(path) != digest:
            raise ValueError(f"Native Codex bundled file checksum mismatch: {name}")
        try:
            # Sandbox setup grants RX on the explicit launcher read root. The
            # file is public, but only this exact hash-verified file may retain
            # such grants; directories, helpers and manifests stay private.
            _owned(path, verified_public_executable=system == "Windows" and name == "bin/codex.exe")
        except ValueError as error:
            raise ValueError(f"Native Codex bundled file security validation failed ({name}): {error}") from error
    if (manifest.get("binary_sha256") != files[executable]
            or type(manifest.get("protocol")) is not int or manifest["protocol"] != 1
            or manifest.get("feature") != "bello_native_selection"
            or manifest.get("transport_timeout_seconds") != 315):
        raise ValueError("Native Codex bundle does not implement the required selection protocol")
    if system == "Windows" and (not isinstance(manifest.get("transports"), list)
                                or "tcp-hmac-v1" not in manifest["transports"]):
        raise ValueError("Native Codex Windows bundle requires the tcp-hmac-v1 selection transport")
    for name in expected_files:
        if not name.startswith("bin/"):
            continue
        if not os.access(directory / name, os.X_OK):
            raise ValueError(f"Native Codex bundled executable is not executable: {name}")


def _download(bundle: NativeBundle, destination: Path) -> None:
    if not bundle.url.startswith("https://github.com/Makson179/Bello/releases/download/"):
        raise ValueError("Native Codex download must use the pinned Bello release")
    request = Request(bundle.url, headers={"User-Agent": "Bello-native-codex-installer"})
    digest = hashlib.sha256()
    size = 0
    with urlopen(request, timeout=60) as response, destination.open("xb") as output:
        if not response.geturl().startswith("https://"):
            raise ValueError("Native Codex download was redirected to an insecure URL")
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > _MAX_DOWNLOAD:
                raise ValueError("Native Codex download exceeds its size limit")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != bundle.archive_sha256:
        raise ValueError("Native Codex archive checksum mismatch")


def _unpack(archive: Path, destination: Path, *, system: str = "Darwin") -> None:
    """Extract only the small, fixed regular-file layout; never tar links/modes."""
    seen = set()
    size = 0
    expected_files = _bundle_files(system)
    expected_directories = _bundle_directories(system)
    _owned(destination, directory=True)
    # Create every allowlisted directory explicitly: mkdir(parents=True) only
    # applies mode=0700 to the leaf, leaving a nested archive's bin/ public when
    # its first member is bin/codex-resources/bwrap.
    for relative in sorted(expected_directories - {"."},
                           key=lambda value: (len(PurePosixPath(value).parts), value)):
        _private_directory(destination / relative)
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            name = member.name.removeprefix("./")
            if member.isdir() and name.rstrip("/") in expected_directories:
                continue
            if (name not in expected_files or name in seen or not member.isfile()
                    or PurePosixPath(name).is_absolute() or member.size < 0):
                raise ValueError(f"Unexpected entry in native Codex archive: {member.name}")
            seen.add(name)
            size += member.size
            if size > _MAX_UNPACKED:
                raise ValueError("Native Codex archive expands beyond its size limit")
            target = destination / name
            _owned(target.parent, directory=True)
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError(f"Unreadable native Codex archive entry: {name}")
            with stream, target.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    output.write(chunk)
            target.chmod(0o700 if name.startswith("bin/") else 0o600)
    if seen != expected_files:
        raise ValueError("Native Codex archive is incomplete")


def ensure_native_selection() -> tuple[list[str], Path | None]:
    """Return native app-server command + manifest, without changing user config."""
    override = os.environ.get("BELLO_CODEX_BINARY")
    explicit_manifest = os.environ.get("BELLO_CODEX_SELECTION_MANIFEST")
    if override or explicit_manifest:
        return ([override or "codex", "app-server", "--listen", "stdio://"],
                Path(explicit_manifest).expanduser().absolute() if explicit_manifest else None)
    machine = platform.machine().lower()
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    key = (platform.system(), machine)
    bundle = BUNDLES.get(key)
    if bundle is None:
        raise RuntimeError(
            f"No verified native Codex log-distiller download is available for {key[0]}/{key[1]}. "
            "Set BELLO_CODEX_BINARY and BELLO_CODEX_SELECTION_MANIFEST to a compatible build; "
            "see docs/native-codex-selection.md. Other engines and distiller-off runs do not need it."
        )
    base = Path(os.environ.get("BELLO_RUNTIME_DIR", str(Path.home() / ".bello" / "runtime"))).expanduser().absolute()
    _reject_windows_reparse_ancestors(base)
    if _IS_WINDOWS:
        _private_directory(base, parents=True)
    else:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
    _owned(base, directory=True)
    root = base / "native-codex"
    _private_directory(root)
    destination = root / bundle.archive_sha256
    lock_path = root / f".{bundle.archive_sha256}.lock"
    if is_link_or_reparse(lock_path):
        raise ValueError("Native Codex installation lock must not be a symbolic link or reparse point")
    if lock_path.exists():
        _owned(lock_path)
    with FileLock(lock_path):
        _owned(lock_path)
        if destination.exists() or is_link_or_reparse(destination):
            # Never repair/replace a changed or active executable underneath a run.
            _verify(destination, bundle, system=key[0])
        else:
            with tempfile.TemporaryDirectory(prefix=".download-", dir=root) as temporary:
                staging = Path(temporary)
                _owned(staging, directory=True, private=True)
                archive = staging / "bundle.tar.gz"
                _download(bundle, archive)
                unpacked = staging / "unpacked"
                unpacked.mkdir(mode=0o700)
                _unpack(archive, unpacked, system=key[0])
                _verify(unpacked, bundle, system=key[0])
                unpacked.rename(destination)
    executable = "codex.exe" if key[0] == "Windows" else "codex"
    return ([str(destination / "bin" / executable), "app-server", "--listen", "stdio://"],
            destination / "selection-manifest.json")
