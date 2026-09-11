//! Explicit, fixed-scope administrative setup, never a privileged command runner.
//!
//! The named capability is not application authentication. Any host program can
//! request it; consequently its sole host grant is non-inheriting root metadata.
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
    GetLastError, ERROR_INSUFFICIENT_BUFFER, PSID, WAIT_ABANDONED, WAIT_OBJECT_0, WAIT_TIMEOUT,
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

fn system_root() -> Result<PathBuf> {
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
    root_from_windows_directory(&directory)
}

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

struct SetupLock(Handle);

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
    let root = system_root()?;
    let handle = open_system_root(&root, write)?;
    let capability = CapabilitySids::system_root_metadata()?;
    let sid = capability.single_sid()?;
    let changed = if write {
        acl::set_system_root_metadata(&handle, sid, operation == "prepare")?
    } else {
        false
    };
    let prepared = acl::system_root_metadata_prepared(&handle, sid)?;
    Ok(HostStatus {
        protocol_version: crate::protocol::PROTOCOL_VERSION,
        kind: "hostPreparation",
        operation,
        system_root: root.to_string_lossy().into_owned(),
        capability_name: SYSTEM_ROOT_METADATA_CAPABILITY,
        capability_sid: sid_string(sid)?,
        metadata_mask: SYSTEM_ROOT_METADATA_MASK,
        prepared,
        changed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

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
