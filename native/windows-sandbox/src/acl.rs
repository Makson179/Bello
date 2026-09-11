use crate::protocol::SandboxMode;
use crate::winutil::{contains, path_eq, walk_pinned_tree, wide, Handle};
use anyhow::{anyhow, Context, Result};
use std::ffi::c_void;
use std::mem;
use std::path::Path;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use windows_sys::Win32::Foundation::{LocalFree, PSID};
use windows_sys::Win32::Security::Authorization::{
    GetSecurityInfo, SetEntriesInAclW, SetSecurityInfo, EXPLICIT_ACCESS_W, SET_ACCESS,
    SE_FILE_OBJECT, TRUSTEE_IS_SID, TRUSTEE_IS_UNKNOWN, TRUSTEE_W,
};
use windows_sys::Win32::Security::{
    AclSizeInformation, AddAccessAllowedAce, AddAce, DeleteAce, EqualSid, GetAce,
    GetAclInformation, GetLengthSid, GetSecurityDescriptorControl, GetSecurityDescriptorDacl,
    GetTokenInformation, InitializeAcl, InitializeSecurityDescriptor, IsValidSid,
    SetSecurityDescriptorControl, SetSecurityDescriptorDacl, SetSecurityDescriptorOwner, TokenUser,
    ACCESS_ALLOWED_ACE, ACCESS_DENIED_ACE, ACE_HEADER, ACL, ACL_SIZE_INFORMATION,
    CONTAINER_INHERIT_ACE, DACL_SECURITY_INFORMATION, INHERITED_ACE, INHERIT_ONLY_ACE,
    OBJECT_INHERIT_ACE, OWNER_SECURITY_INFORMATION, PROTECTED_DACL_SECURITY_INFORMATION,
    SECURITY_ATTRIBUTES, SECURITY_DESCRIPTOR, SE_DACL_AUTO_INHERITED, SE_DACL_AUTO_INHERIT_REQ,
    SE_DACL_DEFAULTED, SE_DACL_PROTECTED, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateDirectoryW, DELETE, FILE_ALL_ACCESS, FILE_GENERIC_EXECUTE, FILE_GENERIC_READ,
    FILE_GENERIC_WRITE, FILE_READ_ATTRIBUTES, FILE_READ_EA, READ_CONTROL, SYNCHRONIZE,
};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

const GRANT_ACCESS: i32 = 1;
#[cfg(test)]
const DENY_ACCESS: i32 = 3;

struct SecurityDescriptor(*mut c_void);

impl Drop for SecurityDescriptor {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                LocalFree(self.0);
            }
        }
    }
}

struct LocalAcl(*mut ACL);

impl Drop for LocalAcl {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                LocalFree(self.0 as *mut c_void);
            }
        }
    }
}

fn trustee(sid: PSID) -> TRUSTEE_W {
    TRUSTEE_W {
        pMultipleTrustee: std::ptr::null_mut(),
        MultipleTrusteeOperation: 0,
        TrusteeForm: TRUSTEE_IS_SID,
        TrusteeType: TRUSTEE_IS_UNKNOWN,
        ptstrName: sid as *mut u16,
    }
}

fn current_dacl(handle: &Handle) -> Result<(*mut ACL, SecurityDescriptor)> {
    let mut dacl: *mut ACL = std::ptr::null_mut();
    let mut descriptor: *mut c_void = std::ptr::null_mut();
    let code = unsafe {
        GetSecurityInfo(
            handle.raw(),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut dacl,
            std::ptr::null_mut(),
            &mut descriptor,
        )
    };
    if code != 0 {
        return Err(anyhow!("GetSecurityInfo failed with Win32 error {code}"));
    }
    let owned = SecurityDescriptor(descriptor);
    if dacl.is_null() {
        return Err(anyhow!(
            "null DACLs cannot be mutated without changing their security semantics"
        ));
    }
    Ok((dacl, owned))
}

pub fn require_non_null_dacl(handle: &Handle) -> Result<()> {
    let _ = raw_object_dacl(handle)?;
    Ok(())
}

fn current_user_sid_buffer() -> Result<Vec<usize>> {
    let mut token = 0;
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return Err(crate::winutil::last_error("OpenProcessToken"));
    }
    let token = Handle::new(token, "OpenProcessToken")?;
    let mut required = 0_u32;
    unsafe {
        GetTokenInformation(
            token.raw(),
            TokenUser,
            std::ptr::null_mut(),
            0,
            &mut required,
        );
    }
    if required < mem::size_of::<TOKEN_USER>() as u32 {
        return Err(anyhow!(
            "GetTokenInformation returned an invalid user SID size"
        ));
    }
    let words = (required as usize).div_ceil(mem::size_of::<usize>());
    let mut buffer = vec![0_usize; words];
    if unsafe {
        GetTokenInformation(
            token.raw(),
            TokenUser,
            buffer.as_mut_ptr() as *mut c_void,
            required,
            &mut required,
        )
    } == 0
    {
        return Err(crate::winutil::last_error("GetTokenInformation(TokenUser)"));
    }
    let user_sid = unsafe { (*(buffer.as_ptr() as *const TOKEN_USER)).User.Sid };
    if user_sid.is_null() {
        return Err(anyhow!("the current process token has no user SID"));
    }
    Ok(buffer)
}

pub fn owner_matches(handle: &Handle, expected: PSID) -> Result<bool> {
    let mut owner = std::ptr::null_mut();
    let mut descriptor = std::ptr::null_mut();
    let code = unsafe {
        GetSecurityInfo(
            handle.raw(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION,
            &mut owner,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut descriptor,
        )
    };
    if code != 0 {
        return Err(anyhow!(
            "GetSecurityInfo(metadata owner) failed with Win32 error {code}"
        ));
    }
    let _descriptor = SecurityDescriptor(descriptor);
    if owner.is_null() {
        return Err(anyhow!("metadata directory has no owner"));
    }
    Ok(unsafe { EqualSid(owner, expected) } != 0)
}

pub fn owned_by_current_account(handle: &Handle) -> Result<bool> {
    let buffer = current_user_sid_buffer()?;
    let sid = unsafe { (*(buffer.as_ptr() as *const TOKEN_USER)).User.Sid };
    owner_matches(handle, sid)
}

fn state_directory_dacl(user_sid: PSID) -> Result<LocalAcl> {
    let entry = EXPLICIT_ACCESS_W {
        grfAccessPermissions: FILE_ALL_ACCESS,
        grfAccessMode: SET_ACCESS,
        grfInheritance: CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE,
        Trustee: trustee(user_sid),
    };
    let mut exact_dacl: *mut ACL = std::ptr::null_mut();
    let code = unsafe { SetEntriesInAclW(1, &entry, std::ptr::null(), &mut exact_dacl) };
    if code != 0 {
        return Err(anyhow!(
            "SetEntriesInAclW(state directory) failed with Win32 error {code}"
        ));
    }
    Ok(LocalAcl(exact_dacl))
}

pub fn create_state_directory(path: &Path) -> Result<()> {
    // Elevated accounts may default new objects to an Administrators owner.
    // Set the intended account owner and protected DACL atomically at creation;
    // never take ownership of or relax validation for an existing directory.
    let buffer = current_user_sid_buffer()?;
    let user_sid = unsafe { (*(buffer.as_ptr() as *const TOKEN_USER)).User.Sid };
    let exact_dacl = state_directory_dacl(user_sid)?;
    let mut descriptor: SECURITY_DESCRIPTOR = unsafe { mem::zeroed() };
    let descriptor_ptr = &mut descriptor as *mut _ as *mut c_void;
    if unsafe { InitializeSecurityDescriptor(descriptor_ptr, 1) } == 0
        || unsafe { SetSecurityDescriptorOwner(descriptor_ptr, user_sid, 0) } == 0
        || unsafe { SetSecurityDescriptorDacl(descriptor_ptr, 1, exact_dacl.0, 0) } == 0
        || unsafe {
            SetSecurityDescriptorControl(descriptor_ptr, SE_DACL_PROTECTED, SE_DACL_PROTECTED)
        } == 0
    {
        return Err(crate::winutil::last_error(
            "initialize state directory security",
        ));
    }
    let attributes = SECURITY_ATTRIBUTES {
        nLength: mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor_ptr,
        bInheritHandle: 0,
    };
    if unsafe { CreateDirectoryW(wide(path).as_ptr(), &attributes) } == 0 {
        return Err(std::io::Error::last_os_error()).context("CreateDirectoryW(state directory)");
    }
    Ok(())
}

pub fn protect_state_directory(handle: &Handle) -> Result<()> {
    let buffer = current_user_sid_buffer()?;
    let user_sid = unsafe { (*(buffer.as_ptr() as *const TOKEN_USER)).User.Sid };

    let mut owner: PSID = std::ptr::null_mut();
    let mut descriptor: *mut c_void = std::ptr::null_mut();
    let code = unsafe {
        GetSecurityInfo(
            handle.raw(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION,
            &mut owner,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut descriptor,
        )
    };
    if code != 0 {
        return Err(anyhow!(
            "GetSecurityInfo(owner) failed with Win32 error {code}"
        ));
    }
    let _descriptor = SecurityDescriptor(descriptor);
    if owner.is_null() || unsafe { EqualSid(owner, user_sid) } == 0 {
        return Err(anyhow!(
            "the Windows sandbox state directory is not owned by the current account"
        ));
    }

    let exact_dacl = state_directory_dacl(user_sid)?;
    let code = unsafe {
        SetSecurityInfo(
            handle.raw(),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            exact_dacl.0,
            std::ptr::null_mut(),
        )
    };
    if code != 0 {
        return Err(anyhow!(
            "SetSecurityInfo(state directory) failed with Win32 error {code}"
        ));
    }

    let (dacl, _descriptor) = current_dacl(handle)?;
    let mut info: ACL_SIZE_INFORMATION = unsafe { mem::zeroed() };
    if unsafe {
        GetAclInformation(
            dacl,
            &mut info as *mut _ as *mut c_void,
            mem::size_of_val(&info) as u32,
            AclSizeInformation,
        )
    } == 0
    {
        return Err(crate::winutil::last_error(
            "GetAclInformation(state directory)",
        ));
    }
    if info.AceCount != 1 {
        return Err(anyhow!(
            "the Windows sandbox state DACL is not restricted to one account"
        ));
    }
    let mut raw: *mut c_void = std::ptr::null_mut();
    if unsafe { GetAce(dacl, 0, &mut raw) } == 0 {
        return Err(crate::winutil::last_error("GetAce(state directory)"));
    }
    let header = unsafe { &*(raw as *const ACE_HEADER) };
    let ace = unsafe { &*(raw as *const ACCESS_ALLOWED_ACE) };
    let ace_sid =
        (raw as usize + mem::size_of::<ACE_HEADER>() + mem::size_of::<u32>()) as *mut c_void;
    if header.AceType != ACCESS_ALLOWED_ACE_TYPE
        || ace.Mask != FILE_ALL_ACCESS
        || unsafe { EqualSid(ace_sid, user_sid) } == 0
    {
        return Err(anyhow!(
            "the Windows sandbox state DACL does not grant exactly its owner"
        ));
    }
    Ok(())
}

#[cfg(test)]
fn set_entries(handle: &Handle, entries: &[EXPLICIT_ACCESS_W]) -> Result<()> {
    let (old_dacl, _descriptor) = current_dacl(handle)?;
    let mut new_dacl: *mut ACL = std::ptr::null_mut();
    let code = unsafe {
        SetEntriesInAclW(
            entries.len() as u32,
            entries.as_ptr(),
            old_dacl,
            &mut new_dacl,
        )
    };
    if code != 0 {
        return Err(anyhow!("SetEntriesInAclW failed with Win32 error {code}"));
    }
    let new_dacl = LocalAcl(new_dacl);
    let code = unsafe {
        SetSecurityInfo(
            handle.raw(),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            new_dacl.0,
            std::ptr::null_mut(),
        )
    };
    if code != 0 {
        return Err(anyhow!("SetSecurityInfo failed with Win32 error {code}"));
    }
    Ok(())
}

pub fn grant_object(
    handle: &Handle,
    sid: PSID,
    mode: SandboxMode,
    allow_delete: bool,
) -> Result<()> {
    let inheritance = CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE;
    let base_mask = FILE_GENERIC_READ
        | FILE_GENERIC_EXECUTE
        | if mode == SandboxMode::WorkspaceWrite {
            FILE_GENERIC_WRITE | if allow_delete { DELETE } else { 0 }
        } else {
            0
        };
    let mut entries = vec![EXPLICIT_ACCESS_W {
        grfAccessPermissions: base_mask,
        grfAccessMode: GRANT_ACCESS,
        grfInheritance: inheritance,
        Trustee: trustee(sid),
    }];
    if mode == SandboxMode::WorkspaceWrite && !allow_delete {
        // DELETE applies to descendants, not the authority root. FILE_DELETE_CHILD
        // is intentionally absent so an ungranted private child stays protected.
        entries.push(EXPLICIT_ACCESS_W {
            grfAccessPermissions: DELETE,
            grfAccessMode: GRANT_ACCESS,
            grfInheritance: inheritance | INHERIT_ONLY_ACE,
            Trustee: trustee(sid),
        });
    }
    set_object_entries(handle, &entries)
}

#[cfg(test)]
pub fn grant(handle: &Handle, sid: PSID, mode: SandboxMode) -> Result<()> {
    grant_object(handle, sid, mode, false)
}

#[cfg(test)]
pub fn deny_all(handle: &Handle, sid: PSID) -> Result<()> {
    set_entries(
        handle,
        &[EXPLICIT_ACCESS_W {
            grfAccessPermissions: FILE_ALL_ACCESS,
            grfAccessMode: DENY_ACCESS,
            grfInheritance: CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE,
            Trustee: trustee(sid),
        }],
    )
}

fn raw_object_dacl(handle: &Handle) -> Result<(*mut ACL, Vec<usize>)> {
    #[link(name = "ntdll")]
    extern "system" {
        fn NtQuerySecurityObject(
            handle: isize,
            information: u32,
            descriptor: *mut c_void,
            length: u32,
            needed: *mut u32,
        ) -> i32;
    }
    let mut required = 0;
    let mut status = unsafe {
        NtQuerySecurityObject(
            handle.raw(),
            DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            0,
            &mut required,
        )
    };
    for _ in 0..3 {
        if required == 0 || required > 128 * 1024 {
            return Err(anyhow!(
                "NtQuerySecurityObject invalid size {required}, NTSTATUS {:#010x}",
                status as u32
            ));
        }
        let mut buffer = vec![0_usize; (required as usize).div_ceil(mem::size_of::<usize>())];
        status = unsafe {
            NtQuerySecurityObject(
                handle.raw(),
                DACL_SECURITY_INFORMATION,
                buffer.as_mut_ptr() as *mut c_void,
                (buffer.len() * mem::size_of::<usize>()) as u32,
                &mut required,
            )
        };
        if status == 0xC0000023_u32 as i32 {
            continue;
        }
        if status < 0 {
            return Err(anyhow!(
                "NtQuerySecurityObject failed with NTSTATUS {:#010x}",
                status as u32
            ));
        }
        let mut dacl = std::ptr::null_mut();
        let mut present = 0;
        let mut defaulted = 0;
        if unsafe {
            GetSecurityDescriptorDacl(
                buffer.as_ptr() as *mut c_void,
                &mut present,
                &mut dacl,
                &mut defaulted,
            )
        } == 0
        {
            return Err(crate::winutil::last_error("GetSecurityDescriptorDacl(raw)"));
        }
        if present == 0 || dacl.is_null() {
            return Err(anyhow!("native sandbox refuses an absent/null DACL"));
        }
        return Ok((dacl, buffer));
    }
    Err(anyhow!("raw DACL changed size repeatedly during probe"))
}

fn set_object_dacl(handle: &Handle, dacl: *mut ACL) -> Result<()> {
    // Native object-local update: never propagate into unvalidated children.
    #[link(name = "ntdll")]
    extern "system" {
        fn NtSetSecurityObject(handle: isize, information: u32, descriptor: *mut c_void) -> i32;
    }
    let (_, raw) = raw_object_dacl(handle)?;
    let mut control = 0;
    let mut revision = 0;
    if unsafe {
        GetSecurityDescriptorControl(raw.as_ptr() as *mut c_void, &mut control, &mut revision)
    } == 0
    {
        return Err(crate::winutil::last_error(
            "GetSecurityDescriptorControl(raw)",
        ));
    }
    let mut descriptor: SECURITY_DESCRIPTOR = unsafe { mem::zeroed() };
    let pointer = &mut descriptor as *mut _ as *mut c_void;
    let preserved = SE_DACL_PROTECTED | SE_DACL_AUTO_INHERITED | SE_DACL_AUTO_INHERIT_REQ;
    // The native setter needs the request bit to retain an existing
    // AUTO_INHERITED state. This set-only bit is absent from queried SDs.
    let requested = (control & preserved)
        | if control & SE_DACL_AUTO_INHERITED != 0 {
            SE_DACL_AUTO_INHERIT_REQ
        } else {
            0
        };
    if unsafe { InitializeSecurityDescriptor(pointer, 1) } == 0
        || unsafe {
            SetSecurityDescriptorDacl(
                pointer,
                1,
                dacl,
                i32::from(control & SE_DACL_DEFAULTED != 0),
            )
        } == 0
        || unsafe { SetSecurityDescriptorControl(pointer, preserved, requested) } == 0
    {
        return Err(crate::winutil::last_error("initialize object-local DACL"));
    }
    let status = unsafe { NtSetSecurityObject(handle.raw(), DACL_SECURITY_INFORMATION, pointer) };
    if status < 0 {
        return Err(anyhow!(
            "NtSetSecurityObject failed with NTSTATUS {:#010x}",
            status as u32
        ));
    }
    Ok(())
}

fn set_object_entries(handle: &Handle, entries: &[EXPLICIT_ACCESS_W]) -> Result<()> {
    let (old_dacl, _descriptor) = raw_object_dacl(handle)?;
    let mut dacl = std::ptr::null_mut();
    let result =
        unsafe { SetEntriesInAclW(entries.len() as u32, entries.as_ptr(), old_dacl, &mut dacl) };
    if result != 0 {
        return Err(anyhow!("SetEntriesInAclW(object-local) failed: {result}"));
    }
    let dacl = LocalAcl(dacl);
    set_object_dacl(handle, dacl.0)
}

pub fn revoke(handle: &Handle, sid: PSID) -> Result<()> {
    let (dacl, _descriptor) = raw_object_dacl(handle)?;
    let mut filtered = dacl_without_access_sid(dacl, sid)?;
    if unsafe { (*dacl).AceCount == (*(filtered.as_ptr() as *const ACL)).AceCount } {
        return Ok(());
    }
    set_object_dacl(handle, filtered.as_mut_ptr() as *mut ACL)
}

// This mask does not include listing, content, traversal, delete, or writes.
// It matches the metadata-only system-drive preparation documented by Microsoft:
// https://github.com/microsoft/mxc/blob/main/docs/host-prep.md
pub const SYSTEM_ROOT_METADATA_MASK: u32 =
    FILE_READ_ATTRIBUTES | FILE_READ_EA | READ_CONTROL | SYNCHRONIZE;

fn metadata_entry_present(dacl: *mut ACL, sid: PSID) -> Result<bool> {
    let mut present = false;
    for index in 0..u32::from(unsafe { (*dacl).AceCount }) {
        let mut raw = std::ptr::null_mut();
        if unsafe { GetAce(dacl, index, &mut raw) } == 0 {
            return Err(crate::winutil::last_error("GetAce(host preparation)"));
        }
        if !ace_has_sid(raw, sid)? {
            continue;
        }
        let header = unsafe { &*(raw as *const ACE_HEADER) };
        if header.AceType != ACCESS_ALLOWED_ACE_TYPE
            || header.AceFlags != 0
            || unsafe { (*(raw as *const ACCESS_ALLOWED_ACE)).Mask } != SYSTEM_ROOT_METADATA_MASK
            || present
        {
            return Err(anyhow!(
                "conflicting system-root capability ACE; no permissions changed"
            ));
        }
        present = true;
    }
    Ok(present)
}

pub fn system_root_metadata_prepared(handle: &Handle, sid: PSID) -> Result<bool> {
    let (dacl, _descriptor) = raw_object_dacl(handle)?;
    metadata_entry_present(dacl, sid)
}

/// The host-preparation caller pins the fixed OS root and holds its admin-only
/// mutation lock. Never use GRANT_ACCESS merging or recursively propagate here.
pub fn set_system_root_metadata(handle: &Handle, sid: PSID, prepared: bool) -> Result<bool> {
    let (dacl, _descriptor) = raw_object_dacl(handle)?;
    let present = metadata_entry_present(dacl, sid)?;
    if present == prepared {
        return Ok(false);
    }
    if !prepared {
        // Presence validation proved the sole matching SID entry is our exact
        // non-inheriting tuple. Foreign entries are copied unchanged.
        let mut filtered = dacl_without_access_sid(dacl, sid)?;
        set_object_dacl(handle, filtered.as_mut_ptr() as *mut ACL)?;
    } else {
        let mut info: ACL_SIZE_INFORMATION = unsafe { mem::zeroed() };
        if unsafe {
            GetAclInformation(
                dacl,
                &mut info as *mut _ as *mut c_void,
                mem::size_of_val(&info) as u32,
                AclSizeInformation,
            )
        } == 0
        {
            return Err(crate::winutil::last_error(
                "GetAclInformation(host preparation)",
            ));
        }
        let ace_size = mem::size_of::<ACCESS_ALLOWED_ACE>() - mem::size_of::<u32>()
            + unsafe { GetLengthSid(sid) } as usize;
        let size = (info.AclBytesInUse as usize)
            .checked_add(ace_size)
            .filter(|value| *value <= u16::MAX as usize)
            .ok_or_else(|| anyhow!("system-root DACL is too large for metadata preparation"))?;
        let mut buffer = vec![0_usize; size.div_ceil(mem::size_of::<usize>())];
        let replacement = buffer.as_mut_ptr() as *mut ACL;
        let revision = u32::from(unsafe { (*dacl).AclRevision });
        if unsafe { InitializeAcl(replacement, size as u32, revision) } == 0 {
            return Err(crate::winutil::last_error(
                "InitializeAcl(host preparation)",
            ));
        }
        let mut inserted = false;
        for index in 0..info.AceCount {
            let mut raw = std::ptr::null_mut();
            if unsafe { GetAce(dacl, index, &mut raw) } == 0 {
                return Err(crate::winutil::last_error("GetAce(host preparation copy)"));
            }
            let header = unsafe { &*(raw as *const ACE_HEADER) };
            if !inserted && u32::from(header.AceFlags) & INHERITED_ACE != 0 {
                if unsafe {
                    AddAccessAllowedAce(replacement, revision, SYSTEM_ROOT_METADATA_MASK, sid)
                } == 0
                {
                    return Err(crate::winutil::last_error(
                        "AddAccessAllowedAce(host preparation)",
                    ));
                }
                inserted = true;
            }
            if unsafe {
                AddAce(
                    replacement,
                    revision,
                    u32::MAX,
                    raw,
                    u32::from(header.AceSize),
                )
            } == 0
            {
                return Err(crate::winutil::last_error("AddAce(host preparation copy)"));
            }
        }
        if !inserted
            && unsafe { AddAccessAllowedAce(replacement, revision, SYSTEM_ROOT_METADATA_MASK, sid) }
                == 0
        {
            return Err(crate::winutil::last_error(
                "AddAccessAllowedAce(host preparation)",
            ));
        }
        set_object_dacl(handle, replacement)?;
    }
    if system_root_metadata_prepared(handle, sid)? != prepared {
        return Err(anyhow!(
            "system-root metadata read-back did not match the requested state"
        ));
    }
    Ok(true)
}

const ACCESS_ALLOWED_ACE_TYPE: u8 = 0;
const ACCESS_DENIED_ACE_TYPE: u8 = 1;

fn ace_has_sid(raw: *mut c_void, sid: PSID) -> Result<bool> {
    let header = unsafe { &*(raw as *const ACE_HEADER) };
    let size = usize::from(header.AceSize);
    let bytes = unsafe { std::slice::from_raw_parts(raw as *const u8, size) };
    let offset = match header.AceType {
        0 | 1 | 9 | 10 => 8,
        5 | 6 | 11 | 12 => {
            if size < 12 {
                return Err(anyhow!("truncated object ACE"));
            }
            let flags = u32::from_le_bytes(bytes[8..12].try_into().unwrap());
            if flags & !3 != 0 {
                return Err(anyhow!("unsupported object ACE flags"));
            }
            12 + if flags & 1 != 0 { 16 } else { 0 } + if flags & 2 != 0 { 16 } else { 0 }
        }
        _ => {
            // Preserve unknown foreign ACEs, but never claim cleanup succeeded
            // if an unsupported form embeds the run SID (including zero masks).
            let sid_bytes =
                unsafe { std::slice::from_raw_parts(sid as *const u8, GetLengthSid(sid) as usize) };
            if sid_bytes.is_empty() || bytes.windows(sid_bytes.len()).any(|part| part == sid_bytes)
            {
                return Err(anyhow!(
                    "unsupported DACL ACE may contain the AppContainer SID"
                ));
            }
            return Ok(false);
        }
    };
    if size < offset + 8 || size < offset + 8 + usize::from(bytes[offset + 1]) * 4 {
        return Err(anyhow!("truncated SID in DACL ACE"));
    }
    let candidate = unsafe { (raw as *mut u8).add(offset) } as PSID;
    if unsafe { IsValidSid(candidate) } == 0 {
        return Err(anyhow!("invalid SID in DACL ACE"));
    }
    Ok(unsafe { EqualSid(candidate, sid) } != 0)
}

fn dacl_has_sid(dacl: *mut ACL, sid: PSID) -> Result<bool> {
    for index in 0..u32::from(unsafe { (*dacl).AceCount }) {
        let mut raw = std::ptr::null_mut();
        if unsafe { GetAce(dacl, index, &mut raw) } == 0 {
            return Err(crate::winutil::last_error("GetAce(SID presence)"));
        }
        if ace_has_sid(raw, sid)? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn dacl_without_access_sid(dacl: *mut ACL, sid: PSID) -> Result<Vec<usize>> {
    let mut info: ACL_SIZE_INFORMATION = unsafe { mem::zeroed() };
    if unsafe {
        GetAclInformation(
            dacl,
            &mut info as *mut _ as *mut c_void,
            mem::size_of_val(&info) as u32,
            AclSizeInformation,
        )
    } == 0
    {
        return Err(crate::winutil::last_error("GetAclInformation(revoke)"));
    }
    let size = unsafe { (*dacl).AclSize } as usize;
    if size < mem::size_of::<ACL>() || info.AclBytesInUse as usize > size {
        return Err(anyhow!("invalid DACL size during revoke"));
    }
    let mut buffer = vec![0_usize; size.div_ceil(mem::size_of::<usize>())];
    unsafe {
        std::ptr::copy_nonoverlapping(dacl as *const u8, buffer.as_mut_ptr() as *mut u8, size);
    }
    let copied = buffer.as_mut_ptr() as *mut ACL;
    // REVOKE_ACCESS removes allow ACEs, not deny ACEs. Delete exactly this
    // run's basic allow/deny entries while preserving all unrelated ACE bytes,
    // ordering and inheritance flags. Work on a copy of the non-null DACL.
    for index in (0..info.AceCount).rev() {
        let mut raw: *mut c_void = std::ptr::null_mut();
        if unsafe { GetAce(copied, index, &mut raw) } == 0 {
            return Err(crate::winutil::last_error("GetAce(revoke)"));
        }
        if ace_has_sid(raw, sid)? && unsafe { DeleteAce(copied, index) } == 0 {
            return Err(crate::winutil::last_error("DeleteAce(revoke)"));
        }
    }
    Ok(buffer)
}

fn masks_for_sid(dacl: *mut ACL, sid: PSID, include_inherit_only: bool) -> Result<(u32, u32)> {
    let mut info: ACL_SIZE_INFORMATION = unsafe { mem::zeroed() };
    if unsafe {
        GetAclInformation(
            dacl,
            &mut info as *mut _ as *mut c_void,
            mem::size_of::<ACL_SIZE_INFORMATION>() as u32,
            AclSizeInformation,
        )
    } == 0
    {
        return Err(crate::winutil::last_error("GetAclInformation"));
    }
    let mut allowed = 0_u32;
    let mut denied = 0_u32;
    for index in 0..info.AceCount {
        let mut raw: *mut c_void = std::ptr::null_mut();
        if unsafe { GetAce(dacl, index, &mut raw) } == 0 {
            return Err(crate::winutil::last_error("GetAce"));
        }
        let header = unsafe { &*(raw as *const ACE_HEADER) };
        if !include_inherit_only && header.AceFlags & INHERIT_ONLY_ACE as u8 != 0 {
            continue;
        }
        if !ace_has_sid(raw, sid)? {
            continue;
        }
        if header.AceType != ACCESS_ALLOWED_ACE_TYPE && header.AceType != ACCESS_DENIED_ACE_TYPE {
            return Err(anyhow!("unexpected conditional/object AppContainer grant"));
        }
        if header.AceType == ACCESS_ALLOWED_ACE_TYPE {
            allowed |= unsafe { (*(raw as *const ACCESS_ALLOWED_ACE)).Mask };
        } else {
            denied |= unsafe { (*(raw as *const ACCESS_DENIED_ACE)).Mask };
        }
    }
    Ok((allowed, denied))
}

pub fn verify_tree(
    root: &Path,
    private_paths: &[std::path::PathBuf],
    sid: PSID,
    mode: SandboxMode,
    cancelled: &Arc<AtomicBool>,
) -> Result<()> {
    walk_pinned_tree(root, false, cancelled, &mut |path, handle| {
        let (dacl, _descriptor) = raw_object_dacl(handle)?;
        if private_paths.iter().any(|private| contains(private, path)) {
            if dacl_has_sid(dacl, sid)? {
                return Err(anyhow!(
                    "private object has an AppContainer ACE: {}",
                    path.display()
                ));
            }
            return Ok(());
        }
        let (allowed, denied) = masks_for_sid(dacl, sid, false)?;
        let mut expected = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE;
        if mode == SandboxMode::WorkspaceWrite {
            expected |= FILE_GENERIC_WRITE;
            if !path_eq(root, path) && !private_paths.iter().any(|private| contains(path, private))
            {
                expected |= DELETE;
            }
        }
        let (inherited, _) = masks_for_sid(dacl, sid, true)?;
        let expected_inherited = expected
            | if mode == SandboxMode::WorkspaceWrite {
                DELETE
            } else {
                0
            };
        if allowed != expected || denied != 0 || inherited != expected_inherited {
            return Err(anyhow!(
                "AppContainer grant does not match object policy: {}",
                path.display()
            ));
        }
        Ok(())
    })
}

pub fn verify_absent_tree(root: &Path, sid: PSID) -> Result<()> {
    walk_pinned_tree(root, false, &AtomicBool::new(false), &mut |path, handle| {
        let (dacl, _descriptor) = raw_object_dacl(handle)?;
        if dacl_has_sid(dacl, sid)? {
            return Err(anyhow!(
                "AppContainer SID ACE remains after cleanup on {}",
                path.display()
            ));
        }
        Ok(())
    })
}

pub fn verify_absent_object(handle: &Handle, sid: PSID) -> Result<()> {
    let (dacl, _descriptor) = raw_object_dacl(handle)?;
    if dacl_has_sid(dacl, sid)? {
        return Err(anyhow!(
            "AppContainer SID ACE remains after exact-object cleanup"
        ));
    }
    Ok(())
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::winutil::open_path;
    use std::time::{SystemTime, UNIX_EPOCH};
    use windows_sys::Win32::Security::{
        AddAccessAllowedAceEx, AddAccessDeniedAceEx, GetSecurityDescriptorControl, InitializeAcl,
        IsValidAcl, ACL_REVISION, INHERITED_ACE,
    };
    use windows_sys::Win32::Storage::FileSystem::FILE_APPEND_DATA;

    fn ace_bytes(dacl: *mut ACL) -> Vec<Vec<u8>> {
        let count = unsafe { (*dacl).AceCount };
        (0..u32::from(count))
            .map(|index| {
                let mut raw: *mut c_void = std::ptr::null_mut();
                assert_ne!(unsafe { GetAce(dacl, index, &mut raw) }, 0);
                let size = unsafe { (*(raw as *const ACE_HEADER)).AceSize } as usize;
                unsafe { std::slice::from_raw_parts(raw as *const u8, size) }.to_vec()
            })
            .collect()
    }

    type DaclSnapshot = (Vec<Vec<u8>>, u16);

    fn object_dacl_snapshot(handle: &Handle) -> Result<DaclSnapshot> {
        let (dacl, descriptor) = current_dacl(handle)?;
        let mut control = 0;
        let mut revision = 0;
        if unsafe { GetSecurityDescriptorControl(descriptor.0, &mut control, &mut revision) } == 0 {
            return Err(crate::winutil::last_error(
                "GetSecurityDescriptorControl(probe)",
            ));
        }
        Ok((ace_bytes(dacl), control))
    }

    fn raw_dacl_snapshot(handle: &Handle) -> Result<DaclSnapshot> {
        let (dacl, buffer) = raw_object_dacl(handle)?;
        let mut control = 0;
        let mut revision = 0;
        if unsafe {
            GetSecurityDescriptorControl(
                buffer.as_ptr() as *mut c_void,
                &mut control,
                &mut revision,
            )
        } == 0
        {
            return Err(crate::winutil::last_error(
                "GetSecurityDescriptorControl(raw probe)",
            ));
        }
        Ok((ace_bytes(dacl), control))
    }

    pub(crate) fn paired_dacl_snapshot(handle: &Handle) -> Result<(DaclSnapshot, DaclSnapshot)> {
        let raw = raw_dacl_snapshot(handle)?;
        let high_level = object_dacl_snapshot(handle)?;
        anyhow::ensure!(
            raw_dacl_snapshot(handle)? == raw,
            "GetSecurityInfo changed the actual object DACL"
        );
        Ok((raw, high_level))
    }

    #[test]
    fn metadata_preparation_is_exact_noninheriting_and_reversible() {
        let profile = crate::identity::random_profile_name().unwrap();
        let base = std::env::temp_dir().join(format!("bello-host-metadata-{profile}"));
        std::fs::create_dir(&base).unwrap();
        let base = std::fs::canonicalize(base).unwrap();
        let existing = base.join("existing.txt");
        std::fs::write(&existing, "private fixture content").unwrap();
        let root = open_path(&base, true).unwrap();
        let child = open_path(&existing, false).unwrap();
        let root_before = paired_dacl_snapshot(&root).unwrap();
        let child_before = paired_dacl_snapshot(&child).unwrap();
        let capability = crate::identity::CapabilitySids::system_root_metadata().unwrap();
        let sid = capability.single_sid().unwrap();
        assert!(!system_root_metadata_prepared(&root, sid).unwrap());
        assert!(set_system_root_metadata(&root, sid, true).unwrap());
        assert!(system_root_metadata_prepared(&root, sid).unwrap());
        assert!(!set_system_root_metadata(&root, sid, true).unwrap());
        assert_eq!(paired_dacl_snapshot(&child).unwrap(), child_before);
        assert!(!system_root_metadata_prepared(&child, sid).unwrap());
        let new_path = base.join("new.txt");
        std::fs::write(&new_path, "new fixture content").unwrap();
        let new_child = open_path(&new_path, false).unwrap();
        assert!(!system_root_metadata_prepared(&new_child, sid).unwrap());
        let (dacl, _descriptor) = raw_object_dacl(&root).unwrap();
        assert_eq!(
            masks_for_sid(dacl, sid, true).unwrap(),
            (SYSTEM_ROOT_METADATA_MASK, 0)
        );
        let without_ours = dacl_without_access_sid(dacl, sid).unwrap();
        assert_eq!(
            ace_bytes(without_ours.as_ptr() as *mut ACL),
            root_before.0 .0
        );
        assert!(set_system_root_metadata(&root, sid, false).unwrap());
        assert!(!set_system_root_metadata(&root, sid, false).unwrap());
        assert_eq!(paired_dacl_snapshot(&root).unwrap(), root_before);
        assert_eq!(paired_dacl_snapshot(&child).unwrap(), child_before);
        drop((root, child, new_child));
        std::fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn metadata_preparation_refuses_to_merge_or_remove_conflicting_capability_rights() {
        let profile = crate::identity::random_profile_name().unwrap();
        let path = std::env::temp_dir().join(format!("bello-host-conflict-{profile}"));
        std::fs::create_dir(&path).unwrap();
        let path = std::fs::canonicalize(path).unwrap();
        let root = open_path(&path, true).unwrap();
        let capability = crate::identity::CapabilitySids::system_root_metadata().unwrap();
        let sid = capability.single_sid().unwrap();
        for (mask, flags) in [
            (FILE_ALL_ACCESS, 0),
            (SYSTEM_ROOT_METADATA_MASK, OBJECT_INHERIT_ACE),
        ] {
            set_object_entries(
                &root,
                &[EXPLICIT_ACCESS_W {
                    grfAccessPermissions: mask,
                    grfAccessMode: GRANT_ACCESS,
                    grfInheritance: flags,
                    Trustee: trustee(sid),
                }],
            )
            .unwrap();
            let before = paired_dacl_snapshot(&root).unwrap();
            assert!(system_root_metadata_prepared(&root, sid).is_err());
            assert!(set_system_root_metadata(&root, sid, true).is_err());
            assert!(set_system_root_metadata(&root, sid, false).is_err());
            assert_eq!(paired_dacl_snapshot(&root).unwrap(), before);
            revoke(&root, sid).unwrap();
        }
        drop(root);
        std::fs::remove_dir(path).unwrap();
    }

    #[test]
    fn native_delete_probe_child() {
        use std::io::Write;
        use windows_sys::Win32::Storage::FileSystem::{
            CreateFileW, DeleteFileW, FindClose, FindFirstFileW, GetFileAttributesW,
            GetFinalPathNameByHandleW, GetFullPathNameW, GetVolumeInformationByHandleW,
            GetVolumeInformationW, FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_READ,
            FILE_SHARE_WRITE, OPEN_EXISTING, VOLUME_NAME_DOS, VOLUME_NAME_NT, WIN32_FIND_DATAW,
        };
        #[link(name = "kernel32")]
        extern "system" {
            fn GetCurrentDirectoryW(length: u32, buffer: *mut u16) -> u32;
        }
        if std::env::var("BELLO_TEST_DELETE_PROBE").as_deref() != Ok("1")
            || std::env::current_exe()
                .unwrap()
                .file_name()
                .and_then(|name| name.to_str())
                != Some("bello-delete-probe.exe")
        {
            return;
        }
        let name = wide("root.txt");
        let mut directory = vec![0_u16; 32768];
        let count = unsafe { GetCurrentDirectoryW(directory.len() as u32, directory.as_mut_ptr()) };
        let error = (count == 0).then(std::io::Error::last_os_error);
        writeln!(
            std::io::stderr(),
            "native cwd count={count}, path={:?}, error={:?}",
            directory
                .get(..count as usize)
                .map(String::from_utf16_lossy),
            error
        )
        .unwrap();
        let mut absolute = vec![0_u16; 32768];
        let count = unsafe {
            GetFullPathNameW(
                name.as_ptr(),
                absolute.len() as u32,
                absolute.as_mut_ptr(),
                std::ptr::null_mut(),
            )
        };
        let error = (count == 0).then(std::io::Error::last_os_error);
        let absolute = absolute
            .get(..count as usize)
            .map(String::from_utf16_lossy)
            .unwrap_or_default();
        writeln!(
            std::io::stderr(),
            "native full path count={count}, path={absolute:?}, error={:?}",
            error
        )
        .unwrap();
        for candidate in [
            "root.txt".to_owned(),
            absolute.clone(),
            format!("\\\\?\\{absolute}"),
        ] {
            let mut found: WIN32_FIND_DATAW = unsafe { mem::zeroed() };
            let find = unsafe { FindFirstFileW(wide(&candidate).as_ptr(), &mut found) };
            let error = if find == -1 {
                Some(std::io::Error::last_os_error())
            } else {
                None
            };
            writeln!(
                std::io::stderr(),
                "native FindFirstFileW {candidate:?}: {error:?}"
            )
            .unwrap();
            if find != -1 {
                unsafe {
                    FindClose(find);
                }
            }
        }
        if let Some(drive) = absolute.get(..3) {
            let result = unsafe {
                GetVolumeInformationW(
                    wide(drive).as_ptr(),
                    std::ptr::null_mut(),
                    0,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    0,
                )
            };
            let error = (result == 0).then(std::io::Error::last_os_error);
            writeln!(
                std::io::stderr(),
                "native volume query {drive:?}: result={result}, error={error:?}"
            )
            .unwrap();
        }
        writeln!(
            std::io::stderr(),
            "native delete probe attributes: {:#x}",
            unsafe { GetFileAttributesW(name.as_ptr()) }
        )
        .unwrap();
        let opened = Handle::new(
            unsafe {
                CreateFileW(
                    name.as_ptr(),
                    DELETE | FILE_READ_ATTRIBUTES,
                    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                    std::ptr::null(),
                    OPEN_EXISTING,
                    0,
                    0,
                )
            },
            "CreateFileW(child DELETE)",
        );
        writeln!(
            std::io::stderr(),
            "native delete probe open DELETE: {:?}",
            opened.as_ref().map(|_| ())
        )
        .unwrap();
        if let Ok(handle) = &opened {
            let result = unsafe {
                GetVolumeInformationByHandleW(
                    handle.raw(),
                    std::ptr::null_mut(),
                    0,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    0,
                )
            };
            let error = (result == 0).then(std::io::Error::last_os_error);
            writeln!(
                std::io::stderr(),
                "native volume query by allowed handle: result={result}, error={error:?}"
            )
            .unwrap();
            for (label, flags) in [("DOS", VOLUME_NAME_DOS), ("NT", VOLUME_NAME_NT)] {
                let mut buffer = vec![0_u16; 32768];
                let count = unsafe {
                    GetFinalPathNameByHandleW(
                        handle.raw(),
                        buffer.as_mut_ptr(),
                        buffer.len() as u32,
                        flags,
                    )
                };
                let error = (count == 0).then(std::io::Error::last_os_error);
                writeln!(
                    std::io::stderr(),
                    "native final path {label}: count={count}, path={:?}, error={:?}",
                    buffer.get(..count as usize).map(String::from_utf16_lossy),
                    error
                )
                .unwrap();
            }
        }
        drop(opened);
        let deleted = if unsafe { DeleteFileW(name.as_ptr()) } == 0 {
            Err(std::io::Error::last_os_error())
        } else {
            Ok(())
        };
        writeln!(std::io::stderr(), "native DeleteFileW: {deleted:?}").unwrap();
        assert!(deleted.is_ok(), "native DeleteFileW failed: {deleted:?}");
    }

    #[test]
    fn object_only_dacl_updates_preserve_peer_aces_control_and_existing_children() {
        let ours =
            crate::identity::derive_profile_sid(&crate::identity::random_profile_name().unwrap())
                .unwrap();
        let peer =
            crate::identity::derive_profile_sid(&crate::identity::random_profile_name().unwrap())
                .unwrap();
        for protected in [false, true] {
            let root = std::env::temp_dir().join(format!(
                "bello-object-dacl-{}-{}-{protected}",
                std::process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            std::fs::create_dir_all(root.join("child")).unwrap();
            let parent = open_path(&root, true).unwrap();
            if protected {
                let (dacl, _descriptor) = current_dacl(&parent).unwrap();
                assert_eq!(
                    unsafe {
                        SetSecurityInfo(
                            parent.raw(),
                            SE_FILE_OBJECT,
                            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                            std::ptr::null_mut(),
                            std::ptr::null_mut(),
                            dacl,
                            std::ptr::null_mut(),
                        )
                    },
                    0
                );
            }
            set_entries(
                &parent,
                &[EXPLICIT_ACCESS_W {
                    grfAccessPermissions: FILE_GENERIC_READ,
                    grfAccessMode: GRANT_ACCESS,
                    grfInheritance: CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE,
                    Trustee: trustee(peer.0),
                }],
            )
            .unwrap();
            let before = object_dacl_snapshot(&parent).unwrap();
            let raw_before = raw_dacl_snapshot(&parent).unwrap();
            assert_eq!(before.1 & SE_DACL_PROTECTED != 0, protected);
            let child = open_path(&root.join("child"), false).unwrap();
            let child_before = object_dacl_snapshot(&child).unwrap();
            let raw_child_before = raw_dacl_snapshot(&child).unwrap();
            set_object_entries(
                &parent,
                &[EXPLICIT_ACCESS_W {
                    grfAccessPermissions: FILE_GENERIC_READ | FILE_GENERIC_WRITE,
                    grfAccessMode: GRANT_ACCESS,
                    grfInheritance: CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE,
                    Trustee: trustee(ours.0),
                }],
            )
            .unwrap();
            let (dacl, _descriptor) = current_dacl(&parent).unwrap();
            let mut without_ours = dacl_without_access_sid(dacl, ours.0).unwrap();
            assert_eq!(
                ace_bytes(without_ours.as_mut_ptr() as *mut ACL),
                before.0,
                "foreign ACE bytes/order changed"
            );
            assert_eq!(
                object_dacl_snapshot(&parent).unwrap().1,
                before.1,
                "DACL control/defaulted flags changed"
            );
            assert_eq!(
                object_dacl_snapshot(&child).unwrap(),
                child_before,
                "object-only update propagated to existing child"
            );
            assert_eq!(raw_dacl_snapshot(&child).unwrap(), raw_child_before);
            revoke(&parent, ours.0).unwrap();
            assert_eq!(object_dacl_snapshot(&parent).unwrap(), before);
            assert_eq!(object_dacl_snapshot(&child).unwrap(), child_before);
            assert_eq!(raw_dacl_snapshot(&parent).unwrap(), raw_before);
            assert_eq!(raw_dacl_snapshot(&child).unwrap(), raw_child_before);
            drop(child);
            drop(parent);
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn object_only_grants_support_new_children_without_exposing_private_files() {
        use crate::identity::{
            create_profile, delete_profile, profile_local_app_data, random_profile_name,
            CapabilitySids,
        };
        use crate::process::{clean_environment, run_child, Job};
        use anyhow::ensure;
        use std::fs;
        use std::io::Write;
        use windows_sys::Win32::Security::{CreateWellKnownSid, WinBuiltinAnyPackageSid};

        let supplied = std::env::temp_dir().join(format!(
            "bello-sparse-probe-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
        ));
        fs::create_dir_all(supplied.join(".supervisor")).unwrap();
        fs::write(supplied.join(".supervisor").join("sentinel.txt"), "PRIVATE").unwrap();
        fs::write(supplied.join("aap-only.txt"), "AAP_ONLY").unwrap();
        fs::create_dir(supplied.join("ordinary")).unwrap();
        fs::write(supplied.join("ordinary").join("existing.txt"), "EXISTING").unwrap();
        let root = fs::canonicalize(&supplied).unwrap();
        let profile = random_profile_name().unwrap();
        let mut capabilities = CapabilitySids::for_network(false).unwrap();
        let sid = create_profile(&profile, &capabilities).unwrap();
        let mut original_objects = Vec::new();

        let operation = (|| -> Result<()> {
            let handle = open_path(&root, true)?;
            // Configure the AAP-only fixture before granting anything on its
            // parent, so fixture setup cannot inherit the package grant.
            let mut aap_sid = [0_usize; 9];
            let mut aap_size = mem::size_of_val(&aap_sid) as u32;
            ensure!(
                unsafe {
                    CreateWellKnownSid(
                        WinBuiltinAnyPackageSid,
                        std::ptr::null_mut(),
                        aap_sid.as_mut_ptr() as PSID,
                        &mut aap_size,
                    )
                } != 0,
                "could not create ALL_APPLICATION_PACKAGES SID"
            );
            set_entries(
                &open_path(&root.join("aap-only.txt"), true)?,
                &[EXPLICIT_ACCESS_W {
                    grfAccessPermissions: FILE_GENERIC_READ,
                    grfAccessMode: GRANT_ACCESS,
                    grfInheritance: 0,
                    Trustee: trustee(aap_sid.as_mut_ptr() as PSID),
                }],
            )?;
            let private_paths = [
                root.join(".supervisor"),
                root.join(".supervisor").join("sentinel.txt"),
                root.join("aap-only.txt"),
            ];
            let private_before = private_paths
                .iter()
                .map(|path| paired_dacl_snapshot(&open_path(path, false)?))
                .collect::<Result<Vec<_>>>()?;
            let mut pending = vec![root.clone()];
            while let Some(path) = pending.pop() {
                if path.is_dir() {
                    for entry in fs::read_dir(&path)? {
                        pending.push(entry?.path());
                    }
                }
                let object = open_path(&path, false)?;
                let identity = crate::winutil::file_identity(&object)?;
                original_objects.push((
                    (identity.volume_serial, identity.file_index),
                    path,
                    paired_dacl_snapshot(&object)?,
                ));
            }
            let root_control_before = raw_dacl_snapshot(&handle)?.1;
            // Inheritable for future children, but do not propagate to any
            // pre-existing object. Production grant remains unchanged.
            set_object_entries(
                &handle,
                &[
                    EXPLICIT_ACCESS_W {
                        grfAccessPermissions: FILE_GENERIC_READ
                            | FILE_GENERIC_WRITE
                            | FILE_GENERIC_EXECUTE,
                        grfAccessMode: GRANT_ACCESS,
                        grfInheritance: CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE,
                        Trustee: trustee(sid.0),
                    },
                    EXPLICIT_ACCESS_W {
                        grfAccessPermissions: DELETE,
                        grfAccessMode: GRANT_ACCESS,
                        grfInheritance: CONTAINER_INHERIT_ACE
                            | OBJECT_INHERIT_ACE
                            | INHERIT_ONLY_ACE,
                        Trustee: trustee(sid.0),
                    },
                ],
            )?;
            ensure!(
                raw_dacl_snapshot(&handle)?.1 == root_control_before,
                "object-only update changed root DACL control flags"
            );
            for (path, before) in private_paths.iter().zip(&private_before) {
                let after = paired_dacl_snapshot(&open_path(path, false)?)?;
                writeln!(
                    std::io::stderr(),
                    "sparse private {}: raw before={:?}, raw after={:?}, GetSecurityInfo before={:?}, after={:?}",
                    path.display(), before.0, after.0, before.1, after.1
                )?;
                ensure!(
                    after.0 == before.0,
                    "root grant changed {} actual DACL: before={before:?}, after={after:?}",
                    path.display()
                );
            }
            for relative in ["ordinary", "ordinary\\existing.txt"] {
                set_object_entries(
                    &open_path(&root.join(relative), true)?,
                    &[EXPLICIT_ACCESS_W {
                        grfAccessPermissions: FILE_GENERIC_READ
                            | FILE_GENERIC_WRITE
                            | FILE_GENERIC_EXECUTE
                            | DELETE,
                        grfAccessMode: GRANT_ACCESS,
                        grfInheritance: CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE,
                        Trustee: trustee(sid.0),
                    }],
                )?;
            }
            let mut environment = clean_environment(&profile_local_app_data(sid.0)?, &root, &[])?;
            let cancelled = Arc::new(AtomicBool::new(false));
            let mut command = |text: &str| -> Result<i32> {
                let job = Job::create()?;
                let outcome = run_child(
                    text,
                    &root,
                    sid.0,
                    &mut capabilities,
                    &mut environment,
                    &job,
                    &cancelled,
                );
                job.terminate(125)?;
                job.ensure_empty()?;
                writeln!(std::io::stderr(), "sparse command {text:?}: {outcome:?}")?;
                outcome
            };
            ensure!(
                command("type .supervisor\\sentinel.txt")? != 0,
                "sparse root exposed pre-existing private file"
            );
            ensure!(
                command("type aap-only.txt")? != 0,
                "LPAC process read a file allowed only to ALL_APPLICATION_PACKAGES"
            );
            ensure!(
                command("ren ordinary moved-ordinary && type moved-ordinary\\existing.txt")? == 0,
                "sparse grants prevented renaming an ordinary existing subtree"
            );
            for step in [
                "mkdir fresh",
                "echo CHILD>fresh\\first.txt",
                "type fresh\\first.txt",
                "ren fresh\\first.txt second.txt",
                "ren fresh renamed",
                "type renamed\\second.txt",
                "echo ROOT>root.txt",
            ] {
                ensure!(command(step)? == 0, "sparse child operation failed: {step}");
            }
            for relative in [".", "renamed"] {
                ensure!(
                    command(&format!("icacls {relative}"))? == 0,
                    "icacls could not read the allowed object DACL"
                );
                let path = if relative == "." {
                    root.clone()
                } else {
                    root.join(relative)
                };
                let before = paired_dacl_snapshot(&open_path(&path, false)?)?;
                ensure!(
                    command(&format!("icacls {relative} /inheritance:e"))? != 0,
                    "child unexpectedly gained WRITE_DAC on {relative}"
                );
                ensure!(
                    paired_dacl_snapshot(&open_path(&path, false)?)? == before,
                    "child modified {relative} DACL or inheritance flags"
                );
            }
            ensure!(
                fs::read_to_string(root.join("renamed").join("second.txt"))?.trim() == "CHILD",
                "child payload changed"
            );
            {
                use std::os::windows::fs::MetadataExt;
                let file = open_path(&root.join("root.txt"), false)?;
                let (dacl, _descriptor) = raw_object_dacl(&file)?;
                writeln!(
                    std::io::stderr(),
                    "before DEL root.txt: attributes={:#x}, effective package masks={:?}, raw={:?}",
                    fs::metadata(root.join("root.txt"))?.file_attributes(),
                    masks_for_sid(dacl, sid.0, false)?,
                    raw_dacl_snapshot(&file)?
                )?;
            }
            let del_result = command("del root.txt")?;
            if del_result != 0 || root.join("root.txt").exists() {
                // Diagnose the actual kernel operation without giving the
                // child extra rights or accepting a failed shell operation.
                let enumeration = command("dir /b root.txt")?;
                writeln!(
                    std::io::stderr(),
                    "CMD enumeration diagnostic: {enumeration}"
                )?;
                for (name, verbatim) in
                    [("absolute-probe.txt", false), ("verbatim-probe.txt", true)]
                {
                    ensure!(
                        command(&format!("echo PROBE>{name}"))? == 0,
                        "could not create CMD probe fixture"
                    );
                    let path = root.join(name);
                    let path = path
                        .to_str()
                        .ok_or_else(|| anyhow!("non-Unicode probe fixture"))?;
                    let spelling = if verbatim {
                        path
                    } else {
                        path.strip_prefix("\\\\?\\").unwrap_or(path)
                    };
                    let _ = command(&format!("dir /b \"{spelling}\""))?;
                    let _ = command(&format!("del \"{spelling}\""))?;
                }
                let mut windows_buffer = vec![0_u16; 32768];
                let count = unsafe {
                    windows_sys::Win32::System::SystemInformation::GetWindowsDirectoryW(
                        windows_buffer.as_mut_ptr(),
                        windows_buffer.len() as u32,
                    )
                };
                ensure!(
                    count > 0 && (count as usize) < windows_buffer.len(),
                    "could not resolve Windows directory"
                );
                let windows = String::from_utf16(&windows_buffer[..count as usize])?;
                let windows = windows.strip_prefix("\\\\?\\").unwrap_or(&windows);
                let _ = command("echo SystemRoot=%SystemRoot% WINDIR=%WINDIR% COMSPEC=%COMSPEC%")?;
                let _ = command(&format!(
                    "set SystemRoot={windows}&& set WINDIR={windows}&& dir /b root.txt"
                ))?;
                let _ = command(&format!(
                    "\"{windows}\\System32\\cmd.exe\" /d /e:off /f:off /c dir /b root.txt"
                ))?;
                ensure!(
                    command("echo PROBE>extensions-probe.txt")? == 0,
                    "could not create extensions probe"
                );
                let _ = command(&format!(
                    "\"{windows}\\System32\\cmd.exe\" /d /e:off /f:off /c del extensions-probe.txt"
                ))?;
                fs::copy(
                    std::env::current_exe()?,
                    root.join("bello-delete-probe.exe"),
                )?;
                let native_result = command("set BELLO_TEST_DELETE_PROBE=1&& bello-delete-probe.exe --exact acl::tests::native_delete_probe_child --nocapture")?;
                writeln!(
                    std::io::stderr(),
                    "standalone DEL={del_result}, direct native child={native_result}"
                )?;
                return Err(anyhow!("standalone DEL failed despite direct native diagnostic: shell={del_result}, native={native_result}"));
            }
            for step in [
                "del renamed\\second.txt",
                "rmdir renamed",
                "mkdir remaining",
                "echo RETAINED>remaining\\kept.txt",
            ] {
                ensure!(command(step)? == 0, "sparse delete/recreate failed: {step}");
            }
            ensure!(
                !root.join("root.txt").exists() && !root.join("renamed").exists(),
                "new objects were not deleted"
            );
            for relative in [
                "remaining",
                "remaining\\kept.txt",
                ".supervisor\\sentinel.txt",
            ] {
                let child = open_path(&root.join(relative), false)?;
                let (dacl, _descriptor) = raw_object_dacl(&child)?;
                let masks = masks_for_sid(dacl, sid.0, true)?;
                // Write directly so successful CI probes retain this evidence.
                writeln!(
                    std::io::stderr(),
                    "sparse probe {relative}: package allow={:#x}, deny={:#x}",
                    masks.0,
                    masks.1
                )?;
                if relative.starts_with(".supervisor") {
                    ensure!(masks == (0, 0), "private file received a package ACE");
                }
            }
            for (path, before) in private_paths.iter().zip(&private_before) {
                ensure!(
                    paired_dacl_snapshot(&open_path(path, false)?)?.0 == before.0,
                    "commands changed actual private DACL bytes or control flags"
                );
            }
            revoke(&handle, sid.0)?;
            let root_only_cleanup = verify_absent_tree(&root, sid.0);
            writeln!(
                std::io::stderr(),
                "sparse probe root-only cleanup: {root_only_cleanup:?}"
            )?;
            Ok(())
        })();

        // Probe cleanup deliberately handles explicit package ACEs on newly
        // created children too. Verify absence rather than hiding leftovers by
        // deleting the fixture. No unrelated SID or inheritance bit changes.
        let cleanup = (|| -> Result<()> {
            let mut paths = Vec::new();
            let mut pending = vec![root.clone()];
            while let Some(path) = pending.pop() {
                if path.is_dir() {
                    for entry in fs::read_dir(&path)? {
                        pending.push(entry?.path());
                    }
                }
                paths.push(path);
            }
            for path in paths.iter().rev() {
                let handle = open_path(path, true)?;
                let (dacl, _descriptor) = raw_object_dacl(&handle)?;
                let masks = masks_for_sid(dacl, sid.0, true)?;
                writeln!(
                    std::io::stderr(),
                    "sparse cleanup {}: package allow={:#x}, deny={:#x}",
                    path.strip_prefix(&root)?.display(),
                    masks.0,
                    masks.1
                )?;
                revoke(&handle, sid.0)?;
            }
            verify_absent_tree(&root, sid.0)?;
            let current_objects = paths
                .iter()
                .map(|path| {
                    let handle = open_path(path, false)?;
                    let identity = crate::winutil::file_identity(&handle)?;
                    Ok(((identity.volume_serial, identity.file_index), path))
                })
                .collect::<Result<Vec<_>>>()?;
            for (identity, original_path, before) in &original_objects {
                let (_, current_path) = current_objects
                    .iter()
                    .find(|(current, _)| current == identity)
                    .ok_or_else(|| {
                        anyhow!("original fixture disappeared: {}", original_path.display())
                    })?;
                let after = paired_dacl_snapshot(&open_path(current_path, false)?)?;
                ensure!(
                    &after == before,
                    "cleanup did not restore raw/high-level DACL for {} (now {}): before={before:?}, after={after:?}",
                    original_path.display(), current_path.display()
                );
            }
            delete_profile(&profile)?;
            fs::remove_dir_all(&root)?;
            Ok(())
        })();
        assert!(cleanup.is_ok(), "sparse probe cleanup failed: {cleanup:?}");
        assert!(operation.is_ok(), "sparse probe failed: {operation:?}");
    }

    #[test]
    fn revoke_filters_only_run_allow_and_deny_aces_without_changing_other_entries() {
        let ours =
            crate::identity::derive_profile_sid(&crate::identity::random_profile_name().unwrap())
                .unwrap();
        let other =
            crate::identity::derive_profile_sid(&crate::identity::random_profile_name().unwrap())
                .unwrap();
        let mut buffer = vec![0_usize; 128];
        let dacl = buffer.as_mut_ptr() as *mut ACL;
        assert_ne!(
            unsafe {
                InitializeAcl(
                    dacl,
                    (buffer.len() * mem::size_of::<usize>()) as u32,
                    ACL_REVISION,
                )
            },
            0
        );
        for (allow, sid, mask, flags) in [
            (
                false,
                ours.0,
                FILE_ALL_ACCESS,
                CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE,
            ),
            (
                false,
                other.0,
                FILE_APPEND_DATA,
                OBJECT_INHERIT_ACE | INHERITED_ACE,
            ),
            (true, ours.0, FILE_GENERIC_READ, 0),
            (
                true,
                ours.0,
                FILE_GENERIC_WRITE,
                CONTAINER_INHERIT_ACE | INHERIT_ONLY_ACE,
            ),
            (true, other.0, FILE_GENERIC_READ, CONTAINER_INHERIT_ACE),
            (false, other.0, DELETE, 0),
        ] {
            let result = unsafe {
                if allow {
                    AddAccessAllowedAceEx(dacl, ACL_REVISION, flags, mask, sid)
                } else {
                    AddAccessDeniedAceEx(dacl, ACL_REVISION, flags, mask, sid)
                }
            };
            assert_ne!(result, 0);
        }
        let before = ace_bytes(dacl);
        let expected = vec![before[1].clone(), before[4].clone(), before[5].clone()];
        let mut filtered = dacl_without_access_sid(dacl, ours.0).unwrap();
        let filtered_dacl = filtered.as_mut_ptr() as *mut ACL;
        assert_eq!(ace_bytes(dacl), before, "source DACL was mutated");
        assert_eq!(ace_bytes(filtered_dacl), expected);
        assert_eq!(masks_for_sid(filtered_dacl, ours.0, true).unwrap(), (0, 0));
        assert_eq!(
            masks_for_sid(filtered_dacl, other.0, true).unwrap(),
            masks_for_sid(dacl, other.0, true).unwrap(),
        );
        let mut repeated = dacl_without_access_sid(filtered_dacl, ours.0).unwrap();
        assert_eq!(ace_bytes(repeated.as_mut_ptr() as *mut ACL), expected);
        let mut empty = dacl_without_access_sid(filtered_dacl, other.0).unwrap();
        assert!(
            !empty.is_empty(),
            "empty ACL must retain its allocated header"
        );
        let empty_dacl = empty.as_mut_ptr() as *mut ACL;
        assert_ne!(unsafe { IsValidAcl(empty_dacl) }, 0);
        assert!(ace_bytes(empty_dacl).is_empty());
    }

    #[test]
    fn revoke_removes_zero_mask_callback_and_object_entries_and_rejects_unknown_owned_aces() {
        use windows_sys::Win32::Security::AddAccessAllowedObjectAce;
        let ours =
            crate::identity::derive_profile_sid(&crate::identity::random_profile_name().unwrap())
                .unwrap();
        let peer =
            crate::identity::derive_profile_sid(&crate::identity::random_profile_name().unwrap())
                .unwrap();
        let mut buffer = vec![0_usize; 128];
        let dacl = buffer.as_mut_ptr() as *mut ACL;
        assert_ne!(
            unsafe { InitializeAcl(dacl, mem::size_of_val(buffer.as_slice()) as u32, 4) },
            0
        );
        assert_ne!(
            unsafe { AddAccessAllowedAceEx(dacl, 4, 0, FILE_GENERIC_READ, peer.0) },
            0
        );
        let peer_before = ace_bytes(dacl);
        assert_ne!(unsafe { AddAccessAllowedAceEx(dacl, 4, 0, 0, ours.0) }, 0);
        let mut raw = std::ptr::null_mut();
        assert_ne!(unsafe { GetAce(dacl, 1, &mut raw) }, 0);
        unsafe {
            (*(raw as *mut ACE_HEADER)).AceType = 9;
        }
        assert_ne!(
            unsafe {
                AddAccessAllowedObjectAce(
                    dacl,
                    4,
                    0,
                    FILE_GENERIC_READ,
                    std::ptr::null(),
                    std::ptr::null(),
                    ours.0,
                )
            },
            0
        );
        assert!(dacl_has_sid(dacl, ours.0).unwrap());
        let mut filtered = dacl_without_access_sid(dacl, ours.0).unwrap();
        let cleaned = filtered.as_mut_ptr() as *mut ACL;
        assert_eq!(ace_bytes(cleaned), peer_before);
        assert!(!dacl_has_sid(cleaned, ours.0).unwrap());
        unsafe {
            (*(raw as *mut ACE_HEADER)).AceType = 254;
        }
        assert!(dacl_without_access_sid(dacl, ours.0).is_err());
        assert!(dacl_has_sid(dacl, ours.0).is_err());
    }

    #[test]
    fn recovery_directory_is_created_with_account_owner_and_protected_dacl() {
        let path = std::env::temp_dir().join(format!(
            "bello-state-owner-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
        ));
        create_state_directory(&path).unwrap();
        let handle = open_path(&path, true).unwrap();
        let buffer = current_user_sid_buffer().unwrap();
        let user_sid = unsafe { (*(buffer.as_ptr() as *const TOKEN_USER)).User.Sid };
        let (dacl, descriptor) = current_dacl(&handle).unwrap();
        let mut control = 0;
        let mut revision = 0;
        assert_ne!(
            unsafe { GetSecurityDescriptorControl(descriptor.0, &mut control, &mut revision) },
            0
        );
        assert_ne!(control & SE_DACL_PROTECTED, 0);
        assert_eq!(
            masks_for_sid(dacl, user_sid, true).unwrap(),
            (FILE_ALL_ACCESS, 0)
        );
        let mut info: ACL_SIZE_INFORMATION = unsafe { mem::zeroed() };
        assert_ne!(
            unsafe {
                GetAclInformation(
                    dacl,
                    &mut info as *mut _ as *mut c_void,
                    mem::size_of::<ACL_SIZE_INFORMATION>() as u32,
                    AclSizeInformation,
                )
            },
            0
        );
        assert_eq!(info.AceCount, 1);
        // The unchanged strict owner check must accept a fresh directory even
        // when the process token's default owner is the Administrators group.
        protect_state_directory(&handle).unwrap();
        assert!(create_state_directory(&path).is_err());
        drop(handle);
        std::fs::remove_dir(path).unwrap();
    }
}
