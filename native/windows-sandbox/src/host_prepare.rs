//! Explicit, fixed-scope administrative setup, never a privileged command runner.
//!
//! The named capability is not application authentication. Any host program can
//! request it; consequently its only host grants are non-inheriting metadata on
//! the two fixed OS targets, never their contents or descendants.
//! Our lock coordinates Bello installers across accounts, not unrelated admin
//! programs changing filesystem security outside this protocol.

use crate::acl::{self, SYSTEM_ROOT_METADATA_MASK};
use crate::identity::{sid_string, CapabilitySids, SYSTEM_ROOT_METADATA_CAPABILITY};
use crate::winutil::{self, wide, Handle};
use anyhow::{anyhow, Result};
use serde::Serialize;
use std::ffi::c_void;
use std::mem;
use std::path::{Path, PathBuf};
use windows_sys::Win32::Foundation::{
    GetLastError, ERROR_INSUFFICIENT_BUFFER, ERROR_MORE_DATA, PSID, WAIT_ABANDONED, WAIT_OBJECT_0,
    WAIT_TIMEOUT,
};
use windows_sys::Win32::Security::{
    AddAccessAllowedAce, CheckTokenMembership, CreateWellKnownSid, EqualSid, GetAce,
    GetKernelObjectSecurity, GetSecurityDescriptorDacl, GetSecurityDescriptorOwner,
    GetTokenInformation, InitializeAcl, InitializeSecurityDescriptor, IsValidSid,
    SetSecurityDescriptorDacl, SetSecurityDescriptorOwner, TokenElevation,
    WinBuiltinAdministratorsSid, WinLocalSystemSid, ACCESS_ALLOWED_ACE, ACE_HEADER, ACL,
    ACL_REVISION, DACL_SECURITY_INFORMATION, OWNER_SECURITY_INFORMATION, SECURITY_ATTRIBUTES,
    SECURITY_DESCRIPTOR, TOKEN_ELEVATION, TOKEN_QUERY, WELL_KNOWN_SID_TYPE,
};
use windows_sys::Win32::Storage::FileSystem::READ_CONTROL;
use windows_sys::Win32::System::Registry::{
    RegGetValueW, HKEY_LOCAL_MACHINE, REG_EXPAND_SZ, REG_SZ, RRF_NOEXPAND, RRF_RT_REG_EXPAND_SZ,
    RRF_RT_REG_SZ, RRF_SUBKEY_WOW6464KEY,
};
use windows_sys::Win32::System::SystemInformation::GetSystemWindowsDirectoryW;
use windows_sys::Win32::System::Threading::{
    CreateMutexExW, GetCurrentProcess, OpenProcessToken, ReleaseMutex, WaitForSingleObject,
    SYNCHRONIZATION_SYNCHRONIZE,
};

const SETUP_LOCK: &str = "Global\\Bello.Sandbox.SystemRootMetadata.Setup.v1";
const SETUP_LOCK_ACCESS: u32 = SYNCHRONIZATION_SYNCHRONIZE | READ_CONTROL;

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HostStatus {
    protocol_version: u32,
    kind: &'static str,
    operation: &'static str,
    system_root: String,
    capability_name: &'static str,
    capability_sid: String,
    metadata_mask: u32,
    prepared: bool,
    changed: bool,
    targets: Vec<HostTargetStatus>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct HostTargetStatus {
    kind: &'static str,
    path: String,
    prepared: bool,
    changed: bool,
}

pub struct FixedTarget {
    kind: &'static str,
    pub path: PathBuf,
}

fn known_sid(kind: WELL_KNOWN_SID_TYPE) -> Result<Vec<usize>> {
    let mut bytes = 68_u32; // SECURITY_MAX_SID_SIZE
    let mut value = vec![0_usize; (bytes as usize).div_ceil(mem::size_of::<usize>())];
    if unsafe {
        CreateWellKnownSid(
            kind,
            std::ptr::null_mut(),
            value.as_mut_ptr() as PSID,
            &mut bytes,
        )
    } == 0
    {
        return Err(winutil::last_error("CreateWellKnownSid(host preparation)"));
    }
    Ok(value)
}

fn require_elevated_admin() -> Result<()> {
    let mut raw = 0;
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw) } == 0 {
        return Err(winutil::last_error("OpenProcessToken(host preparation)"));
    }
    let token = Handle::new(raw, "OpenProcessToken(host preparation)")?;
    let mut elevation: TOKEN_ELEVATION = unsafe { mem::zeroed() };
    let mut returned = 0;
    if unsafe {
        GetTokenInformation(
            token.raw(),
            TokenElevation,
            &mut elevation as *mut _ as *mut c_void,
            mem::size_of_val(&elevation) as u32,
            &mut returned,
        )
    } == 0
    {
        return Err(winutil::last_error("GetTokenInformation(TokenElevation)"));
    }
    let admin = known_sid(WinBuiltinAdministratorsSid)?;
    let mut is_admin = 0;
    if unsafe { CheckTokenMembership(0, admin.as_ptr() as PSID, &mut is_admin) } == 0 {
        return Err(winutil::last_error(
            "CheckTokenMembership(host preparation)",
        ));
    }
    if elevation.TokenIsElevated == 0 || is_admin == 0 {
        return Err(anyhow!("host-prepare and host-remove require an elevated Administrator terminal; no permissions changed"));
    }
    Ok(())
}

fn root_from_windows_directory(directory: &str) -> Result<PathBuf> {
    let bytes = directory.as_bytes();
    if bytes.len() < 4
        || !bytes[0].is_ascii_alphabetic()
        || &bytes[1..3] != b":\\"
        || !winutil::is_normalized_local_absolute(Path::new(directory))
    {
        return Err(anyhow!(
            "the OS Windows directory is not an ordinary local drive path"
        ));
    }
    Ok(PathBuf::from(format!(
        "{}:\\",
        (bytes[0] as char).to_ascii_uppercase()
    )))
}

fn system_windows_directory() -> Result<String> {
    // Do not read SystemDrive/SystemRoot from an elevated process's environment.
    let mut buffer = vec![0_u16; 32768];
    let length = unsafe { GetSystemWindowsDirectoryW(buffer.as_mut_ptr(), buffer.len() as u32) };
    if length == 0 {
        return Err(winutil::last_error("GetSystemWindowsDirectoryW"));
    }
    if length as usize >= buffer.len() {
        return Err(anyhow!("OS Windows directory exceeds the supported size"));
    }
    let directory = String::from_utf16(&buffer[..length as usize])?;
    root_from_windows_directory(&directory)?;
    Ok(directory)
}

#[cfg(test)]
fn system_root() -> Result<PathBuf> {
    root_from_windows_directory(&system_windows_directory()?)
}

fn profiles_directory_value() -> Result<(String, bool)> {
    // Microsoft's User Profiles support tooling uses this exact machine value:
    // https://learn.microsoft.com/en-us/troubleshoot/windows-server/support-tools/scripts-to-cleanup-profile-folder-information-and-prevent-temp-user-profiles-from-being-created
    // NOEXPAND is essential: the helper normally has an empty environment and
    // an elevated setup must never trust caller-provided SystemDrive/TEMP/etc.
    let key = wide(r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList");
    let value = wide("ProfilesDirectory");
    let flags = RRF_RT_REG_SZ | RRF_RT_REG_EXPAND_SZ | RRF_NOEXPAND | RRF_SUBKEY_WOW6464KEY;
    let mut size = 0;
    let mut kind = 0;
    let result = unsafe {
        RegGetValueW(
            HKEY_LOCAL_MACHINE,
            key.as_ptr(),
            value.as_ptr(),
            flags,
            &mut kind,
            std::ptr::null_mut(),
            &mut size,
        )
    };
    if result != 0 {
        return Err(anyhow!(
            "RegGetValueW(ProfilesDirectory size) failed with Win32 error {result}"
        ));
    }
    for _ in 0..3 {
        if !(2..=65536).contains(&size) || size % 2 != 0 {
            return Err(anyhow!("invalid machine ProfilesDirectory string size"));
        }
        let mut buffer = vec![0_u16; size as usize / 2 + 1];
        let mut bytes = (buffer.len() * 2) as u32;
        let result = unsafe {
            RegGetValueW(
                HKEY_LOCAL_MACHINE,
                key.as_ptr(),
                value.as_ptr(),
                flags,
                &mut kind,
                buffer.as_mut_ptr() as *mut c_void,
                &mut bytes,
            )
        };
        if matches!(result, ERROR_MORE_DATA | ERROR_INSUFFICIENT_BUFFER) {
            size = bytes;
            continue;
        }
        if result != 0 {
            return Err(anyhow!(
                "RegGetValueW(ProfilesDirectory) failed with Win32 error {result}"
            ));
        }
        if !matches!(kind, REG_SZ | REG_EXPAND_SZ)
            || bytes < 2
            || bytes % 2 != 0
            || bytes as usize > buffer.len() * 2
        {
            return Err(anyhow!(
                "invalid machine ProfilesDirectory string type or length"
            ));
        }
        let units = &buffer[..bytes as usize / 2];
        if units.last() != Some(&0) || units[..units.len() - 1].contains(&0) {
            return Err(anyhow!(
                "machine ProfilesDirectory is not one terminated string"
            ));
        }
        return Ok((
            String::from_utf16(&units[..units.len() - 1])?,
            kind == REG_EXPAND_SZ,
        ));
    }
    Err(anyhow!("machine ProfilesDirectory changed size repeatedly"))
}

fn expand_profiles_directory(value: &str, expandable: bool, windows: &str) -> Result<PathBuf> {
    let root = root_from_windows_directory(windows)?;
    let drive = root
        .to_str()
        .ok_or_else(|| anyhow!("invalid OS drive"))?
        .trim_end_matches('\\');
    let mut expanded = String::new();
    let mut remaining = value;
    while expandable && remaining.contains('%') {
        let start = remaining.find('%').expect("a percent is present");
        expanded.push_str(&remaining[..start]);
        let variable = &remaining[start + 1..];
        let end = variable
            .find('%')
            .ok_or_else(|| anyhow!("unterminated ProfilesDirectory variable"))?;
        let name = &variable[..end];
        if name.eq_ignore_ascii_case("SystemDrive") {
            expanded.push_str(drive);
        } else if name.eq_ignore_ascii_case("SystemRoot") || name.eq_ignore_ascii_case("windir") {
            expanded.push_str(windows);
        } else {
            return Err(anyhow!("unsupported machine ProfilesDirectory variable; caller environment is never expanded"));
        }
        remaining = &variable[end + 1..];
    }
    expanded.push_str(remaining);
    // Also rejects UNC, device paths, parent traversal, NUL and a volume root.
    root_from_windows_directory(&expanded)?;
    let path = PathBuf::from(expanded);
    if winutil::is_volume_root(&path) {
        return Err(anyhow!(
            "machine ProfilesDirectory must not be a volume root"
        ));
    }
    Ok(path)
}

#[cfg(test)]
fn open_system_root(root: &Path, write: bool) -> Result<Handle> {
    if !winutil::is_volume_root(root) {
        return Err(anyhow!(
            "host preparation target must be the literal OS drive root"
        ));
    }
    let expected = winutil::verbatim_local_absolute(root)?;
    let handle = winutil::open_path(&expected, write)?;
    winutil::validate_final_path(&handle, &expected)?;
    let identity = winutil::validate_plain_file_object(&handle, root)?;
    if identity.file_index == 0 {
        return Err(anyhow!("OS drive root has no stable file identity"));
    }
    winutil::require_persistent_acls(&handle, root)?;
    Ok(handle)
}

/// These are fixed OS locations, never caller-selected directories. A relocated
/// UserProfiles folder remains a named target, not permission to grant its tree.
pub fn fixed_targets() -> Result<Vec<FixedTarget>> {
    let windows = system_windows_directory()?;
    let root = root_from_windows_directory(&windows)?;
    let (value, expandable) = profiles_directory_value()?;
    let profiles = expand_profiles_directory(&value, expandable, &windows)?;
    Ok(vec![
        FixedTarget {
            kind: "systemDriveRoot",
            path: root,
        },
        FixedTarget {
            kind: "userProfiles",
            path: profiles,
        },
    ])
}

pub struct SetupLock(Handle);

/// The normal helper may change exact metadata on its own directories under its
/// existing account lock. Administrators-owned ancestors additionally require an
/// already elevated caller and this same cross-account lock. Never elevate here.
pub fn metadata_mutation_lock(handle: &Handle) -> Result<Option<SetupLock>> {
    if acl::owned_by_current_account(handle)? {
        return Ok(None);
    }
    let admin = known_sid(WinBuiltinAdministratorsSid)?;
    if !acl::owner_matches(handle, admin.as_ptr() as PSID)? {
        return Err(anyhow!("metadata ancestor is not owned by this account or Administrators; no permissions changed"));
    }
    require_elevated_admin()?;
    let lock = SetupLock::acquire()?;
    if !acl::owner_matches(handle, admin.as_ptr() as PSID)? {
        return Err(anyhow!("metadata ancestor owner changed during setup"));
    }
    Ok(Some(lock))
}

impl SetupLock {
    fn acquire() -> Result<Self> {
        let admin = known_sid(WinBuiltinAdministratorsSid)?;
        let system = known_sid(WinLocalSystemSid)?;
        let mut storage = [0_usize; 32];
        let dacl = storage.as_mut_ptr() as *mut ACL;
        let mut descriptor: SECURITY_DESCRIPTOR = unsafe { mem::zeroed() };
        let sd = &mut descriptor as *mut _ as *mut c_void;
        if unsafe { InitializeAcl(dacl, mem::size_of_val(&storage) as u32, ACL_REVISION) } == 0
            || unsafe {
                AddAccessAllowedAce(
                    dacl,
                    ACL_REVISION,
                    SETUP_LOCK_ACCESS,
                    admin.as_ptr() as PSID,
                )
            } == 0
            || unsafe {
                AddAccessAllowedAce(
                    dacl,
                    ACL_REVISION,
                    SETUP_LOCK_ACCESS,
                    system.as_ptr() as PSID,
                )
            } == 0
            || unsafe { InitializeSecurityDescriptor(sd, 1) } == 0
            || unsafe { SetSecurityDescriptorOwner(sd, admin.as_ptr() as PSID, 0) } == 0
            || unsafe { SetSecurityDescriptorDacl(sd, 1, dacl, 0) } == 0
        {
            return Err(winutil::last_error("initialize administrative setup lock"));
        }
        let attributes = SECURITY_ATTRIBUTES {
            nLength: mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: sd,
            bInheritHandle: 0,
        };
        let handle = Handle::new(
            unsafe { CreateMutexExW(&attributes, wide(SETUP_LOCK).as_ptr(), 0, SETUP_LOCK_ACCESS) },
            "CreateMutexExW(administrative setup lock)",
        )?;
        // CreateMutexEx ignores the proposed descriptor for an existing name.
        // Validate its actual owner and ACL before trusting the shared lock.
        validate_setup_lock(&handle, admin.as_ptr() as PSID, system.as_ptr() as PSID)?;
        match unsafe { WaitForSingleObject(handle.raw(), 30_000) } {
            WAIT_OBJECT_0 | WAIT_ABANDONED => Ok(Self(handle)),
            WAIT_TIMEOUT => Err(anyhow!(
                "another host preparation is active; timed out without changing permissions"
            )),
            _ => Err(winutil::last_error(
                "WaitForSingleObject(administrative setup lock)",
            )),
        }
    }
}

impl Drop for SetupLock {
    fn drop(&mut self) {
        unsafe {
            ReleaseMutex(self.0.raw());
        }
    }
}

fn validate_setup_lock(handle: &Handle, admin: PSID, system: PSID) -> Result<()> {
    let information = OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION;
    let mut required = 0;
    let size_result = unsafe {
        GetKernelObjectSecurity(
            handle.raw(),
            information,
            std::ptr::null_mut(),
            0,
            &mut required,
        )
    };
    if size_result != 0
        || unsafe { GetLastError() } != ERROR_INSUFFICIENT_BUFFER
        || required == 0
        || required > 65536
    {
        return Err(anyhow!("invalid administrative setup lock descriptor size"));
    }
    let mut storage = vec![0_usize; (required as usize).div_ceil(mem::size_of::<usize>())];
    let sd = storage.as_mut_ptr() as *mut c_void;
    if unsafe { GetKernelObjectSecurity(handle.raw(), information, sd, required, &mut required) }
        == 0
    {
        return Err(winutil::last_error(
            "GetKernelObjectSecurity(administrative setup lock)",
        ));
    }
    let mut owner = std::ptr::null_mut();
    let mut dacl: *mut ACL = std::ptr::null_mut();
    let mut present = 0;
    let mut defaulted = 0;
    if unsafe { GetSecurityDescriptorOwner(sd, &mut owner, &mut defaulted) } == 0
        || owner.is_null()
        || unsafe { EqualSid(owner, admin) } == 0
        || unsafe { GetSecurityDescriptorDacl(sd, &mut present, &mut dacl, &mut defaulted) } == 0
        || present == 0
        || dacl.is_null()
        || unsafe { (*dacl).AceCount } != 2
    {
        return Err(anyhow!(
            "untrusted administrative setup lock; no permissions changed"
        ));
    }
    let mut seen = [false; 2];
    for index in 0..2 {
        let mut raw = std::ptr::null_mut();
        if unsafe { GetAce(dacl, index, &mut raw) } == 0 {
            return Err(winutil::last_error("GetAce(administrative setup lock)"));
        }
        let header = unsafe { &*(raw as *const ACE_HEADER) };
        if header.AceType != 0 || header.AceFlags != 0 || header.AceSize < 16 {
            return Err(anyhow!("untrusted administrative setup lock permissions"));
        }
        let bytes =
            unsafe { std::slice::from_raw_parts(raw as *const u8, usize::from(header.AceSize)) };
        if 16 + 4 * usize::from(bytes[9]) != bytes.len() {
            return Err(anyhow!("invalid administrative setup lock trustee size"));
        }
        let ace = unsafe { &*(raw as *const ACCESS_ALLOWED_ACE) };
        if ace.Mask != SETUP_LOCK_ACCESS {
            return Err(anyhow!("untrusted administrative setup lock mask"));
        }
        let sid = &ace.SidStart as *const _ as PSID;
        if unsafe { IsValidSid(sid) } == 0 {
            return Err(anyhow!("invalid administrative setup lock trustee"));
        }
        let matched = if unsafe { EqualSid(sid, admin) } != 0 {
            0
        } else if unsafe { EqualSid(sid, system) } != 0 {
            1
        } else {
            return Err(anyhow!("untrusted administrative setup lock trustee"));
        };
        if seen[matched] {
            return Err(anyhow!("duplicate administrative setup lock trustee"));
        }
        seen[matched] = true;
    }
    Ok(())
}

pub fn execute(operation: &'static str) -> Result<HostStatus> {
    let write = match operation {
        "status" => false,
        "prepare" | "remove" => true,
        _ => return Err(anyhow!("unknown fixed host preparation operation")),
    };
    if write {
        require_elevated_admin()?;
    }
    let _lock = if write {
        Some(SetupLock::acquire()?)
    } else {
        None
    };
    let fixed = fixed_targets()?;
    // Pin and validate every target before making even the first change.
    // This preserves a pre-existing grant if another target has a conflict.
    let pins = fixed
        .iter()
        .map(|target| winutil::pin_directory_chain(&target.path, write))
        .collect::<Result<Vec<_>>>()?;
    let capability = CapabilitySids::system_root_metadata()?;
    let sid = capability.single_sid()?;
    let handles = pins
        .iter()
        .map(|chain| &chain.last().expect("fixed target has a directory pin").1)
        .collect::<Vec<_>>();
    let before = handles
        .iter()
        .map(|handle| acl::system_root_metadata_prepared(handle, sid))
        .collect::<Result<Vec<_>>>()?;
    let mut changed = vec![false; fixed.len()];
    if write {
        for (index, handle) in handles.iter().enumerate() {
            match acl::set_system_root_metadata(handle, sid, operation == "prepare") {
                Ok(value) => changed[index] = value,
                Err(error) => {
                    let mut failures = vec![format!("host preparation failed: {error:#}")];
                    // The failing setter may have reached readback. Restore only
                    // tuples this operation intended to change, never a full ACL.
                    for rollback in (0..=index).rev() {
                        if before[rollback] == (operation == "prepare") {
                            continue;
                        }
                        if let Err(error) =
                            acl::set_system_root_metadata(handles[rollback], sid, before[rollback])
                        {
                            failures.push(format!(
                                "rollback {} failed: {error:#}",
                                fixed[rollback].path.display()
                            ));
                        }
                    }
                    return Err(anyhow!(failures.join("; ")));
                }
            }
        }
    }
    let targets = fixed
        .iter()
        .enumerate()
        .map(|(index, target)| HostTargetStatus {
            kind: target.kind,
            path: target.path.to_string_lossy().into_owned(),
            prepared: if write {
                operation == "prepare"
            } else {
                before[index]
            },
            changed: changed[index],
        })
        .collect::<Vec<_>>();
    Ok(HostStatus {
        protocol_version: crate::protocol::PROTOCOL_VERSION,
        kind: "hostPreparation",
        operation,
        system_root: fixed[0].path.to_string_lossy().into_owned(),
        capability_name: SYSTEM_ROOT_METADATA_CAPABILITY,
        capability_sid: sid_string(sid)?,
        metadata_mask: SYSTEM_ROOT_METADATA_MASK,
        prepared: targets.iter().all(|target| target.prepared),
        changed: targets.iter().any(|target| target.changed),
        targets,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profiles_directory_expands_only_os_derived_variables() {
        for (input, expected) in [
            (r"%SystemDrive%\Users", r"D:\Users"),
            (r"%sYsTeMdRiVe%\Profiles", r"D:\Profiles"),
            (r"%SystemRoot%\Profiles", r"D:\Windows\Profiles"),
            (r"%windir%\Profiles", r"D:\Windows\Profiles"),
            (r"E:\Profiles", r"E:\Profiles"),
        ] {
            assert_eq!(
                expand_profiles_directory(input, true, r"D:\Windows").unwrap(),
                PathBuf::from(expected)
            );
        }
        assert_eq!(
            expand_profiles_directory(r"E:\Profiles", false, r"D:\Windows").unwrap(),
            PathBuf::from(r"E:\Profiles")
        );
        for rejected in [
            r"%TEMP%\Profiles",
            r"%USERPROFILE%\Profiles",
            r"%SystemDrive\Profiles",
            r"%SystemDrive%\..\Profiles",
            r"%SystemDrive%\",
            r"%SystemDrive%\\",
            r"\\server\Profiles",
            r"\\?\C:\Profiles",
            r"C:Profiles",
        ] {
            assert!(
                expand_profiles_directory(rejected, true, r"D:\Windows").is_err(),
                "accepted {rejected}"
            );
        }
        // REG_SZ is literal, not expanded against either the OS or caller env.
        assert!(expand_profiles_directory(r"%SystemDrive%\Users", false, r"D:\Windows").is_err());
    }

    #[test]
    fn host_status_child() {
        // A separate test process executes the exact host-status implementation.
        // Do not mutate global env in this multithreaded native test binary.
        println!(
            "\nBELLO_HOST_STATUS={}",
            serde_json::to_string(&execute("status").unwrap()).unwrap()
        );
    }

    #[test]
    fn fixed_host_targets_ignore_empty_and_poisoned_environment() {
        let expected = serde_json::to_value(execute("status").unwrap()).unwrap();
        for poisoned in [false, true] {
            let mut child = std::process::Command::new(std::env::current_exe().unwrap());
            child
                .args([
                    "--exact",
                    "host_prepare::tests::host_status_child",
                    "--nocapture",
                ])
                .env_clear();
            if poisoned {
                child
                    .env("SystemDrive", "Z:")
                    .env("SystemRoot", r"Z:\bello-not-the-windows-directory")
                    .env("windir", r"Z:\bello-not-the-windows-directory")
                    .env("TEMP", r"Z:\bello-not-the-profiles-directory");
            }
            let output = child.output().unwrap();
            assert!(
                output.status.success(),
                "host-status failed (poisoned={poisoned}): stdout={} stderr={}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            let stdout = String::from_utf8(output.stdout).unwrap();
            let json = stdout
                .lines()
                .find_map(|line| line.strip_prefix("BELLO_HOST_STATUS="))
                .expect("host-status child must execute and report its actual targets");
            let actual: serde_json::Value = serde_json::from_str(json).unwrap();
            assert_eq!(actual["systemRoot"], expected["systemRoot"]);
            assert_eq!(actual["capabilitySid"], expected["capabilitySid"]);
            assert_eq!(actual["targets"], expected["targets"]);
        }
    }

    #[test]
    fn host_target_is_only_the_literal_os_drive_root() {
        assert_eq!(
            root_from_windows_directory(r"C:\Windows").unwrap(),
            PathBuf::from(r"C:\")
        );
        assert_eq!(
            root_from_windows_directory(r"d:\Windows").unwrap(),
            PathBuf::from(r"D:\")
        );
        for rejected in [
            r"\\server\Windows",
            r"\\?\C:\Windows",
            r"C:Windows",
            r"C:\..\Windows",
            r"C:\Windows.",
            r"Windows",
            "C:\\Win\0dows",
        ] {
            assert!(
                root_from_windows_directory(rejected).is_err(),
                "accepted {rejected}"
            );
        }
        let root = system_root().unwrap();
        assert!(winutil::is_volume_root(&root));
        assert_eq!(root.as_os_str().len(), 3);
        open_system_root(&root, false).unwrap();
        let targets = fixed_targets().unwrap();
        assert_eq!(targets.len(), 2);
        assert_eq!(targets[0].kind, "systemDriveRoot");
        assert_eq!(targets[1].kind, "userProfiles");
        for target in targets {
            assert!(!target.path.to_str().unwrap().starts_with(r"\\?\"));
            winutil::pin_directory_chain(&target.path, false).unwrap();
        }
    }

    #[test]
    fn metadata_capability_is_stable_and_distinct_from_package_grants() {
        let a = CapabilitySids::system_root_metadata().unwrap();
        let b = CapabilitySids::system_root_metadata().unwrap();
        assert_ne!(
            unsafe { EqualSid(a.single_sid().unwrap(), b.single_sid().unwrap()) },
            0
        );
        let text = sid_string(a.single_sid().unwrap()).unwrap();
        assert!(text.starts_with("S-1-15-3-"));
        assert_ne!(text, "S-1-15-2-1");
        assert_ne!(text, "S-1-15-2-2");
        assert_eq!(SYSTEM_ROOT_METADATA_MASK, 0x120088);
    }
}
