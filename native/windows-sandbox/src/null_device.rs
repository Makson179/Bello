//! Explicit, boot-lifetime access to the single Windows null device.
//!
//! This is not a filesystem grant or an elevation path for model commands.
//! Only our exact capability ACE is changed; the device's label is untouched.

use crate::global_acl_lock::GlobalAclLock;
use crate::identity::{sid_string, CapabilitySids, NULL_DEVICE_CAPABILITY};
use crate::winutil::{self, wide, Handle};
use anyhow::{anyhow, Result};
use serde::Serialize;
use std::ffi::c_void;
use std::mem;
use windows_sys::Win32::Foundation::{LocalFree, PSID, UNICODE_STRING};
use windows_sys::Win32::Security::Authorization::{
    GetSecurityInfo, SetSecurityInfo, SE_KERNEL_OBJECT,
};
use windows_sys::Win32::Security::{
    AddAccessAllowedAce, AddAce, GetAce, GetLengthSid, GetSecurityDescriptorControl,
    GetSecurityDescriptorSacl, InitializeAcl, IsValidAcl, ACCESS_ALLOWED_ACE, ACE_HEADER, ACL,
    DACL_SECURITY_INFORMATION, GROUP_SECURITY_INFORMATION, INHERITED_ACE,
    LABEL_SECURITY_INFORMATION, OWNER_SECURITY_INFORMATION,
};
use windows_sys::Win32::Storage::FileSystem::{
    GetFileType, FILE_GENERIC_READ, FILE_GENERIC_WRITE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    FILE_TYPE_CHAR, READ_CONTROL, WRITE_DAC,
};
use windows_sys::Win32::System::IO::IO_STATUS_BLOCK;

pub const PATH: &str = r"\Device\Null";
pub const ACCESS_MASK: u32 = FILE_GENERIC_READ | FILE_GENERIC_WRITE;

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct NullStatus {
    protocol_version: u32,
    kind: &'static str,
    operation: &'static str,
    path: &'static str,
    capability_name: &'static str,
    capability_sid: String,
    access_mask: u32,
    prepared: bool,
    changed: bool,
    lifetime: &'static str,
}

#[repr(C)]
struct ObjectAttributes {
    length: u32,
    root_directory: isize,
    object_name: *mut UNICODE_STRING,
    attributes: u32,
    security_descriptor: *mut c_void,
    security_quality_of_service: *mut c_void,
}

// NtOpenFile is a documented user-mode API for existing devices. A literal NT
// name and OBJ_DONT_REPARSE avoid per-user DOS aliases and path redirection.
// https://learn.microsoft.com/windows/win32/api/winternl/nf-winternl-ntopenfile
// https://learn.microsoft.com/windows/win32/api/ntdef/ns-ntdef-_object_attributes
fn open_null(access: u32) -> Result<Handle> {
    #[link(name = "ntdll")]
    extern "system" {
        fn NtOpenFile(
            handle: *mut isize,
            access: u32,
            attributes: *const ObjectAttributes,
            status: *mut IO_STATUS_BLOCK,
            share: u32,
            options: u32,
        ) -> i32;
    }
    let mut name = wide(PATH);
    let mut unicode = UNICODE_STRING {
        Length: ((name.len() - 1) * 2) as u16,
        MaximumLength: (name.len() * 2) as u16,
        Buffer: name.as_mut_ptr(),
    };
    let attributes = ObjectAttributes {
        length: mem::size_of::<ObjectAttributes>() as u32,
        root_directory: 0,
        object_name: &mut unicode,
        attributes: 0x40 | 0x1000, // OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE, not INHERIT
        security_descriptor: std::ptr::null_mut(),
        security_quality_of_service: std::ptr::null_mut(),
    };
    let mut raw = 0;
    let mut io_status: IO_STATUS_BLOCK = unsafe { mem::zeroed() };
    let status = unsafe {
        NtOpenFile(
            &mut raw,
            access,
            &attributes,
            &mut io_status,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            0,
        )
    };
    if status < 0 {
        return Err(anyhow!(
            "NtOpenFile({PATH}, access={access:#x}) failed with NTSTATUS {:#010x}",
            status as u32
        ));
    }
    let handle = Handle::new(raw, "NtOpenFile(null device)")?;
    // The fixed, non-reparsed NT path pins identity; this independently rejects
    // ordinary files/disks/pipes. Never use filesystem final-path APIs for NUL.
    if unsafe { GetFileType(handle.raw()) } != FILE_TYPE_CHAR {
        return Err(anyhow!("{PATH} is not the expected character device"));
    }
    Ok(handle)
}

struct Descriptor {
    raw: *mut c_void,
    dacl: *mut ACL,
    owner: PSID,
    group: PSID,
}

impl Drop for Descriptor {
    fn drop(&mut self) {
        if !self.raw.is_null() {
            unsafe {
                LocalFree(self.raw);
            }
        }
    }
}

impl Descriptor {
    fn read(handle: &Handle) -> Result<Self> {
        let mut value = Self {
            raw: std::ptr::null_mut(),
            dacl: std::ptr::null_mut(),
            owner: std::ptr::null_mut(),
            group: std::ptr::null_mut(),
        };
        let result = unsafe {
            GetSecurityInfo(
                handle.raw(),
                SE_KERNEL_OBJECT,
                OWNER_SECURITY_INFORMATION
                    | GROUP_SECURITY_INFORMATION
                    | DACL_SECURITY_INFORMATION
                    | LABEL_SECURITY_INFORMATION,
                &mut value.owner,
                &mut value.group,
                &mut value.dacl,
                std::ptr::null_mut(),
                &mut value.raw,
            )
        };
        if result != 0 {
            return Err(anyhow!(
                "GetSecurityInfo(null device) failed with Win32 error {result}"
            ));
        }
        if value.raw.is_null() || value.dacl.is_null() || unsafe { IsValidAcl(value.dacl) } == 0 {
            return Err(anyhow!(
                "null-device preparation refuses an absent/null/invalid DACL"
            ));
        }
        Ok(value)
    }

    fn prepared(&self, sid: PSID) -> Result<bool> {
        let mut found = false;
        for raw in ace_pointers(self.dacl)? {
            if !crate::acl::ace_has_sid(raw, sid)? {
                continue;
            }
            let header = unsafe { &*(raw as *const ACE_HEADER) };
            if found
                || header.AceType != 0
                || header.AceFlags != 0
                || usize::from(header.AceSize) != 8 + unsafe { GetLengthSid(sid) } as usize
                || unsafe { (*(raw as *const ACCESS_ALLOWED_ACE)).Mask } != ACCESS_MASK
            {
                return Err(anyhow!(
                    "conflicting null-device capability ACE; no automatic repair"
                ));
            }
            found = true;
        }
        Ok(found)
    }

    fn snapshot(&self) -> Result<Snapshot> {
        let mut control = 0;
        let mut revision = 0;
        if unsafe { GetSecurityDescriptorControl(self.raw, &mut control, &mut revision) } == 0 {
            return Err(winutil::last_error(
                "GetSecurityDescriptorControl(null device)",
            ));
        }
        let mut sacl = std::ptr::null_mut();
        let mut present = 0;
        let mut defaulted = 0;
        if unsafe { GetSecurityDescriptorSacl(self.raw, &mut present, &mut sacl, &mut defaulted) }
            == 0
        {
            return Err(winutil::last_error(
                "GetSecurityDescriptorSacl(null device label)",
            ));
        }
        Ok(Snapshot {
            owner: sid_string(self.owner)?,
            group: sid_string(self.group)?,
            control,
            label_present: present != 0,
            label: if sacl.is_null() {
                None
            } else {
                Some(ace_bytes(sacl)?)
            },
            dacl_revision: unsafe { (*self.dacl).AclRevision },
            entries: ace_bytes(self.dacl)?,
        })
    }
}

#[derive(Debug, PartialEq, Eq)]
struct Snapshot {
    owner: String,
    group: String,
    control: u16,
    label_present: bool,
    label: Option<Vec<Vec<u8>>>,
    dacl_revision: u8,
    entries: Vec<Vec<u8>>,
}

fn ace_pointers(dacl: *mut ACL) -> Result<Vec<*mut c_void>> {
    if dacl.is_null() || unsafe { IsValidAcl(dacl) } == 0 {
        return Err(anyhow!("invalid ACL in null-device descriptor"));
    }
    (0..u32::from(unsafe { (*dacl).AceCount }))
        .map(|index| {
            let mut raw = std::ptr::null_mut();
            if unsafe { GetAce(dacl, index, &mut raw) } == 0 {
                return Err(winutil::last_error("GetAce(null device)"));
            }
            Ok(raw)
        })
        .collect()
}

fn ace_bytes(dacl: *mut ACL) -> Result<Vec<Vec<u8>>> {
    Ok(ace_pointers(dacl)?
        .into_iter()
        .map(|raw| {
            let size = unsafe { (*(raw as *const ACE_HEADER)).AceSize } as usize;
            unsafe { std::slice::from_raw_parts(raw as *const u8, size) }.to_vec()
        })
        .collect())
}

fn replacement(descriptor: &Descriptor, sid: PSID, prepared: bool) -> Result<Vec<usize>> {
    descriptor.prepared(sid)?; // Reject every nonexact/duplicate owned ACE before mutation.
    let entries = ace_pointers(descriptor.dacl)?;
    let size = mem::size_of::<ACL>()
        + entries
            .iter()
            .map(|raw| unsafe { (*(*raw as *const ACE_HEADER)).AceSize } as usize)
            .sum::<usize>();
    let extra = mem::size_of::<ACCESS_ALLOWED_ACE>() - 4 + unsafe { GetLengthSid(sid) } as usize;
    let capacity = size
        .checked_add(extra)
        .filter(|size| *size <= u16::MAX as usize)
        .ok_or_else(|| anyhow!("null-device DACL is too large"))?;
    let mut storage = vec![0_usize; capacity.div_ceil(mem::size_of::<usize>())];
    let dacl = storage.as_mut_ptr() as *mut ACL;
    let revision = u32::from(unsafe { (*descriptor.dacl).AclRevision });
    if unsafe { InitializeAcl(dacl, capacity as u32, revision) } == 0 {
        return Err(winutil::last_error("InitializeAcl(null device)"));
    }
    let mut inserted = !prepared;
    for raw in entries {
        if crate::acl::ace_has_sid(raw, sid)? {
            continue;
        }
        let header = unsafe { &*(raw as *const ACE_HEADER) };
        if !inserted && u32::from(header.AceFlags) & INHERITED_ACE != 0 {
            if unsafe { AddAccessAllowedAce(dacl, revision, ACCESS_MASK, sid) } == 0 {
                return Err(winutil::last_error("AddAccessAllowedAce(null device)"));
            }
            inserted = true;
        }
        if unsafe { AddAce(dacl, revision, u32::MAX, raw, u32::from(header.AceSize)) } == 0 {
            return Err(winutil::last_error("AddAce(null device foreign entry)"));
        }
    }
    if !inserted && unsafe { AddAccessAllowedAce(dacl, revision, ACCESS_MASK, sid) } == 0 {
        return Err(winutil::last_error("AddAccessAllowedAce(null device)"));
    }
    Ok(storage)
}

fn write_dacl(handle: &Handle, storage: &mut [usize]) -> Result<()> {
    let result = unsafe {
        SetSecurityInfo(
            handle.raw(),
            SE_KERNEL_OBJECT,
            DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            storage.as_mut_ptr() as *mut ACL,
            std::ptr::null_mut(),
        )
    };
    if result != 0 {
        return Err(anyhow!(
            "SetSecurityInfo(null device DACL) failed with Win32 error {result}"
        ));
    }
    Ok(())
}

fn set_prepared(handle: &Handle, sid: PSID, prepared: bool) -> Result<bool> {
    let _lock = GlobalAclLock::acquire()?;
    let original = Descriptor::read(handle)?;
    let was_prepared = original.prepared(sid)?;
    if was_prepared == prepared {
        return Ok(false);
    }
    let mut expected = original.snapshot()?;
    let mut changed_dacl = replacement(&original, sid, prepared)?;
    expected.entries = ace_bytes(changed_dacl.as_mut_ptr() as *mut ACL)?;
    let applied = write_dacl(handle, &mut changed_dacl).and_then(|()| {
        let actual = Descriptor::read(handle)?;
        if actual.prepared(sid)? != prepared || actual.snapshot()? != expected {
            return Err(anyhow!("null-device readback changed more than the exact capability ACE; mandatory label is never adjusted automatically"));
        }
        Ok(())
    });
    if let Err(error) = applied {
        // No wholesale descriptor restoration: re-read current state and undo
        // only our exact tuple, retaining any unrelated external admin changes.
        let rollback = (|| -> Result<()> {
            let current = Descriptor::read(handle)?;
            let mut dacl = replacement(&current, sid, was_prepared)?;
            write_dacl(handle, &mut dacl)?;
            if Descriptor::read(handle)?.prepared(sid)? != was_prepared {
                return Err(anyhow!("capability rollback readback failed"));
            }
            Ok(())
        })();
        return match rollback {
            Ok(()) => Err(error.context("null-device operation failed; own capability tuple rolled back")),
            Err(rollback) => Err(anyhow!("null-device operation failed: {error:#}; rollback also failed: {rollback:#}; inspect host status before retrying")),
        };
    }
    Ok(true)
}

pub fn execute(operation: &str) -> Result<NullStatus> {
    let operation = match operation {
        "status" => "status",
        "prepare" => "prepare",
        "remove" => "remove",
        _ => return Err(anyhow!("invalid fixed null-device operation")),
    };
    if operation != "status" {
        crate::host_prepare::require_elevated_admin()?;
    }
    let capability = CapabilitySids::null_device()?;
    let sid = capability.single_sid()?;
    let handle = open_null(READ_CONTROL | if operation == "status" { 0 } else { WRITE_DAC })?;
    let changed = if operation == "status" {
        false
    } else {
        set_prepared(&handle, sid, operation == "prepare")?
    };
    let prepared = Descriptor::read(&handle)?.prepared(sid)?;
    Ok(NullStatus {
        protocol_version: 1,
        kind: "nullDevicePreparation",
        operation,
        path: PATH,
        capability_name: NULL_DEVICE_CAPABILITY,
        capability_sid: sid_string(sid)?,
        access_mask: ACCESS_MASK,
        prepared,
        changed,
        lifetime: "untilReboot",
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{identity, protocol, windows};
    use std::fs;
    use std::io::{Read, Write};
    use std::process::{Command, Stdio};
    use windows_sys::Win32::Foundation::{GetLastError, ERROR_ACCESS_DENIED, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, DELETE, FILE_EXECUTE, OPEN_EXISTING, WRITE_OWNER,
    };
    use windows_sys::Win32::System::Threading::CreateEventW;

    #[test]
    fn fixed_null_identity_and_readonly_status() {
        assert_eq!(ACCESS_MASK, 0x12019f);
        let report = execute("status").unwrap();
        assert_eq!(report.path, PATH);
        assert_eq!(report.kind, "nullDevicePreparation");
        assert_eq!(report.lifetime, "untilReboot");
        assert!(!report.changed);
        let handle = open_null(READ_CONTROL).unwrap();
        assert_eq!(unsafe { GetFileType(handle.raw()) }, FILE_TYPE_CHAR);
        Descriptor::read(&handle).unwrap().snapshot().unwrap();
    }

    #[test]
    fn exact_null_tuple_roundtrip_preserves_every_foreign_entry_and_label() {
        // Only a fresh test SID is changed; never remove the real prepared
        // capability while other native/Python tests are using NUL.
        crate::host_prepare::require_elevated_admin().unwrap();
        let sid = identity::derive_profile_sid(&identity::random_profile_name().unwrap()).unwrap();
        let handle = open_null(READ_CONTROL | WRITE_DAC).unwrap();
        let _lock = GlobalAclLock::acquire().unwrap();
        let before = Descriptor::read(&handle).unwrap().snapshot().unwrap();
        assert!(!Descriptor::read(&handle).unwrap().prepared(sid.0).unwrap());
        struct Cleanup<'a>(&'a Handle, PSID);
        impl Drop for Cleanup<'_> {
            fn drop(&mut self) {
                let _ = set_prepared(self.0, self.1, false);
            }
        }
        let cleanup = Cleanup(&handle, sid.0);
        assert!(set_prepared(&handle, sid.0, true).unwrap());
        assert!(!set_prepared(&handle, sid.0, true).unwrap());
        assert!(set_prepared(&handle, sid.0, false).unwrap());
        assert!(!set_prepared(&handle, sid.0, false).unwrap());
        assert_eq!(
            Descriptor::read(&handle).unwrap().snapshot().unwrap(),
            before
        );
        drop(cleanup);
    }

    #[test]
    fn conflicting_owned_kernel_tuple_is_not_repaired() {
        // A disposable, unnamed kernel object exercises malformed-tuple refusal
        // without placing conflicting permissions on the real host null device.
        // Do not inherit the test runner's ambient default DACL: CI can have a
        // null default DACL, which production correctly refuses to mutate.
        use windows_sys::Win32::Security::Authorization::ConvertStringSecurityDescriptorToSecurityDescriptorW;
        use windows_sys::Win32::Security::{
            GetTokenInformation, TokenPrimaryGroup, SECURITY_ATTRIBUTES, TOKEN_PRIMARY_GROUP,
            TOKEN_QUERY,
        };
        use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};
        let owner = crate::acl::current_account_sid_string().unwrap();
        let mut raw_token = 0;
        assert_ne!(
            unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw_token) },
            0
        );
        let token = Handle::new(raw_token, "test event creator token").unwrap();
        let mut required = 0;
        unsafe {
            GetTokenInformation(
                token.raw(),
                TokenPrimaryGroup,
                std::ptr::null_mut(),
                0,
                &mut required,
            );
        }
        assert!((mem::size_of::<TOKEN_PRIMARY_GROUP>() as u32..=4096).contains(&required));
        let mut group_storage =
            vec![0_usize; (required as usize).div_ceil(mem::size_of::<usize>())];
        assert_ne!(
            unsafe {
                GetTokenInformation(
                    token.raw(),
                    TokenPrimaryGroup,
                    group_storage.as_mut_ptr() as *mut c_void,
                    required,
                    &mut required,
                )
            },
            0
        );
        let group = sid_string(unsafe {
            (*(group_storage.as_ptr() as *const TOKEN_PRIMARY_GROUP)).PrimaryGroup
        })
        .unwrap();
        let sddl = format!("O:{owner}G:{group}D:P(A;;GA;;;{owner})");
        let mut raw_descriptor = std::ptr::null_mut();
        assert_ne!(
            unsafe {
                ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    wide(&sddl).as_ptr(),
                    1,
                    &mut raw_descriptor,
                    std::ptr::null_mut(),
                )
            },
            0
        );
        struct TestDescriptor(*mut c_void);
        impl Drop for TestDescriptor {
            fn drop(&mut self) {
                unsafe {
                    LocalFree(self.0);
                }
            }
        }
        let security = TestDescriptor(raw_descriptor);
        let attributes = SECURITY_ATTRIBUTES {
            nLength: mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: security.0,
            bInheritHandle: 0,
        };
        let handle = Handle::new(
            unsafe { CreateEventW(&attributes, 0, 0, std::ptr::null()) },
            "null ACL test event",
        )
        .unwrap();
        let sid = identity::derive_profile_sid(&identity::random_profile_name().unwrap()).unwrap();
        let _lock = GlobalAclLock::acquire().unwrap();
        let descriptor = Descriptor::read(&handle).unwrap();
        let initial = descriptor.snapshot().unwrap();
        assert_eq!(initial.owner, owner);
        assert_eq!(initial.group, group);
        assert_eq!(initial.entries.len(), 1);
        let mut modified = replacement(&descriptor, sid.0, true).unwrap();
        for raw in ace_pointers(modified.as_mut_ptr() as *mut ACL).unwrap() {
            if crate::acl::ace_has_sid(raw, sid.0).unwrap() {
                unsafe {
                    (*(raw as *mut ACCESS_ALLOWED_ACE)).Mask |= DELETE;
                }
            }
        }
        write_dacl(&handle, &mut modified).unwrap();
        let before = Descriptor::read(&handle).unwrap().snapshot().unwrap();
        for prepared in [true, false] {
            let error = set_prepared(&handle, sid.0, prepared).unwrap_err();
            assert!(
                error
                    .to_string()
                    .contains("conflicting null-device capability ACE"),
                "{error:#}"
            );
        }
        assert_eq!(
            Descriptor::read(&handle).unwrap().snapshot().unwrap(),
            before
        );
    }

    #[test]
    fn actual_lpac_null_reopen_child() {
        if std::env::var("BELLO_TEST_LPAC_NULL").as_deref() != Ok("1") {
            return;
        }
        let mut input = fs::File::open("NUL").unwrap();
        assert_eq!(input.read(&mut [0_u8; 4]).unwrap(), 0);
        fs::OpenOptions::new()
            .write(true)
            .open("NUL")
            .unwrap()
            .write_all(b"Bello LPAC null-device proof")
            .unwrap();
        let mut both = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("NUL")
            .unwrap();
        assert_eq!(both.read(&mut [0_u8; 4]).unwrap(), 0);
        both.write_all(b"discarded").unwrap();
        for access in [WRITE_DAC, WRITE_OWNER, DELETE, FILE_EXECUTE] {
            let raw = unsafe {
                CreateFileW(
                    wide("NUL").as_ptr(),
                    access,
                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                    std::ptr::null(),
                    OPEN_EXISTING,
                    0,
                    0,
                )
            };
            let error = unsafe { GetLastError() };
            assert_eq!(
                raw, INVALID_HANDLE_VALUE,
                "LPAC received NUL rights {access:#x}"
            );
            assert_eq!(error, ERROR_ACCESS_DENIED);
        }
        let error = fs::read_to_string(".supervisor/secret.txt").unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::PermissionDenied);
        fs::write("result.txt", "workspace write remains available").unwrap();
    }

    #[test]
    fn actual_lpac_can_reopen_null_without_device_administration_or_private_reads() {
        assert!(
            execute("status").unwrap().prepared,
            "run explicit admin host-prepare --null-device before this integration test"
        );
        let root = std::env::temp_dir().join(format!(
            "bello-null-lpac-{}",
            identity::random_profile_name().unwrap()
        ));
        fs::create_dir(&root).unwrap();
        fs::create_dir(root.join(".supervisor")).unwrap();
        fs::write(root.join(".supervisor/secret.txt"), "private").unwrap();
        fs::copy(
            std::env::current_exe().unwrap(),
            root.join("null-probe.exe"),
        )
        .unwrap();
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "null_device::tests::lpac_null_launcher_child",
                "--nocapture",
            ])
            .env("BELLO_TEST_LPAC_NULL_ROOT", &root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let keepalive = child.stdin.take().unwrap();
        let output = child.wait_with_output().unwrap();
        drop(keepalive);
        assert!(output.status.success(), "LPAC NUL proof failed: {output:?}");
        assert_eq!(
            fs::read_to_string(root.join(".supervisor/secret.txt")).unwrap(),
            "private"
        );
        assert!(root.join("result.txt").is_file());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn lpac_null_launcher_child() {
        let Some(root) = std::env::var_os("BELLO_TEST_LPAC_NULL_ROOT") else {
            return;
        };
        let root = fs::canonicalize(root).unwrap();
        let path = root
            .to_str()
            .unwrap()
            .strip_prefix(r"\\?\")
            .unwrap()
            .to_owned();
        let private = format!(r"{}\.supervisor", path);
        let result = windows::execute(protocol::Request::Run {
            protocol_version: protocol::PROTOCOL_VERSION,
            command: "set BELLO_TEST_LPAC_NULL=1&& null-probe.exe --exact null_device::tests::actual_lpac_null_reopen_child --nocapture".to_owned(),
            cwd: path.clone(), root: path, mode: protocol::SandboxMode::WorkspaceWrite,
            readable_roots: Vec::new(), private_paths: vec![private], network_access: false,
        });
        assert_eq!(result.unwrap(), 0);
    }
}
