//! One cross-account/session lock for all cooperating filesystem ACL updates.
//!
//! Rights here apply only to this mutex, never to filesystem objects. Ordinary
//! authenticated helpers and elevated helpers use the same name and descriptor.
//! OWNER RIGHTS removes the creator's implicit WRITE_DAC. An unrelated local
//! authenticated process can hold the mutex and cause a bounded timeout (DoS),
//! but cannot authorize a file grant or make helpers proceed without the lock.

use crate::winutil::{self, wide, Handle};
use anyhow::{anyhow, Result};
use std::ffi::c_void;
use std::mem;
use windows_sys::Win32::Foundation::{
    GetLastError, ERROR_INSUFFICIENT_BUFFER, PSID, WAIT_ABANDONED, WAIT_OBJECT_0, WAIT_TIMEOUT,
};
use windows_sys::Win32::Security::{
    AddAccessAllowedAce, CreateWellKnownSid, EqualSid, GetAce, GetKernelObjectSecurity,
    GetSecurityDescriptorDacl, GetSecurityDescriptorOwner, InitializeAcl,
    InitializeSecurityDescriptor, IsValidSid, SetSecurityDescriptorDacl, WinAuthenticatedUserSid,
    WinCreatorOwnerRightsSid, WinLocalSystemSid, ACCESS_ALLOWED_ACE, ACE_HEADER, ACL, ACL_REVISION,
    DACL_SECURITY_INFORMATION, OWNER_SECURITY_INFORMATION, SECURITY_ATTRIBUTES,
    SECURITY_DESCRIPTOR, WELL_KNOWN_SID_TYPE,
};
use windows_sys::Win32::Storage::FileSystem::READ_CONTROL;
use windows_sys::Win32::System::Threading::{
    CreateMutexExW, ReleaseMutex, WaitForSingleObject, SYNCHRONIZATION_SYNCHRONIZE,
};

const NAME: &str = "Global\\Bello.Sandbox.ObjectAclMutations.v1";
const ACCESS: u32 = SYNCHRONIZATION_SYNCHRONIZE | READ_CONTROL;
const WAIT_MS: u32 = 30_000;

pub struct GlobalAclLock {
    handle: Handle,
    // Mutex ownership is thread-local; a guard must not move to another thread.
    _thread: std::marker::PhantomData<std::rc::Rc<()>>,
}

fn trustee(kind: WELL_KNOWN_SID_TYPE) -> Result<Vec<usize>> {
    let mut bytes = 68_u32;
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
        return Err(winutil::last_error("CreateWellKnownSid(metadata lock)"));
    }
    Ok(value)
}

fn open_lock() -> Result<Handle> {
    open_named_lock(NAME)
}

fn open_named_lock(name: &str) -> Result<Handle> {
    let trustees = [
        (trustee(WinAuthenticatedUserSid)?, ACCESS),
        (trustee(WinLocalSystemSid)?, ACCESS),
        (trustee(WinCreatorOwnerRightsSid)?, READ_CONTROL),
    ];
    let mut storage = [0_usize; 32];
    let dacl = storage.as_mut_ptr() as *mut ACL;
    let mut descriptor: SECURITY_DESCRIPTOR = unsafe { mem::zeroed() };
    let sd = &mut descriptor as *mut _ as *mut c_void;
    if unsafe { InitializeAcl(dacl, mem::size_of_val(&storage) as u32, ACL_REVISION) } == 0 {
        return Err(winutil::last_error("InitializeAcl(metadata lock)"));
    }
    for (sid, mask) in &trustees {
        if unsafe { AddAccessAllowedAce(dacl, ACL_REVISION, *mask, sid.as_ptr() as PSID) } == 0 {
            return Err(winutil::last_error("AddAccessAllowedAce(metadata lock)"));
        }
    }
    if unsafe { InitializeSecurityDescriptor(sd, 1) } == 0
        || unsafe { SetSecurityDescriptorDacl(sd, 1, dacl, 0) } == 0
    {
        return Err(winutil::last_error("initialize metadata lock security"));
    }
    let attributes = SECURITY_ATTRIBUTES {
        nLength: mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: sd,
        bInheritHandle: 0,
    };
    let handle = Handle::new(
        unsafe { CreateMutexExW(&attributes, wide(name).as_ptr(), 0, ACCESS) },
        "CreateMutexExW(metadata lock)",
    )?;
    // Existing names ignore the proposed descriptor. Validate the actual object
    // and reject foreign permissions; do not repair a pre-created object.
    validate(&handle, &trustees)?;
    Ok(handle)
}

fn validate(handle: &Handle, trustees: &[(Vec<usize>, u32)]) -> Result<()> {
    let information = OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION;
    let mut bytes = 0;
    let result = unsafe {
        GetKernelObjectSecurity(
            handle.raw(),
            information,
            std::ptr::null_mut(),
            0,
            &mut bytes,
        )
    };
    if result != 0
        || unsafe { GetLastError() } != ERROR_INSUFFICIENT_BUFFER
        || bytes == 0
        || bytes > 65536
    {
        return Err(anyhow!("invalid metadata lock descriptor size"));
    }
    let mut storage = vec![0_usize; (bytes as usize).div_ceil(mem::size_of::<usize>())];
    let sd = storage.as_mut_ptr() as *mut c_void;
    if unsafe { GetKernelObjectSecurity(handle.raw(), information, sd, bytes, &mut bytes) } == 0 {
        return Err(winutil::last_error(
            "GetKernelObjectSecurity(metadata lock)",
        ));
    }
    let mut owner = std::ptr::null_mut();
    let mut dacl: *mut ACL = std::ptr::null_mut();
    let mut defaulted = 0;
    let mut present = 0;
    if unsafe { GetSecurityDescriptorOwner(sd, &mut owner, &mut defaulted) } == 0
        || owner.is_null()
        || unsafe { IsValidSid(owner) } == 0
        || unsafe { GetSecurityDescriptorDacl(sd, &mut present, &mut dacl, &mut defaulted) } == 0
        || present == 0
        || dacl.is_null()
        || unsafe { (*dacl).AceCount } as usize != trustees.len()
    {
        return Err(anyhow!("untrusted metadata lock owner or DACL"));
    }
    let mut seen = vec![false; trustees.len()];
    for index in 0..trustees.len() {
        let mut raw = std::ptr::null_mut();
        if unsafe { GetAce(dacl, index as u32, &mut raw) } == 0 {
            return Err(winutil::last_error("GetAce(metadata lock)"));
        }
        let header = unsafe { &*(raw as *const ACE_HEADER) };
        if header.AceType != 0 || header.AceFlags != 0 || header.AceSize < 16 {
            return Err(anyhow!("untrusted metadata lock ACE"));
        }
        let bytes =
            unsafe { std::slice::from_raw_parts(raw as *const u8, usize::from(header.AceSize)) };
        if 16 + 4 * usize::from(bytes[9]) != bytes.len() {
            return Err(anyhow!("invalid metadata lock trustee size"));
        }
        let ace = unsafe { &*(raw as *const ACCESS_ALLOWED_ACE) };
        let sid = &ace.SidStart as *const _ as PSID;
        if unsafe { IsValidSid(sid) } == 0 {
            return Err(anyhow!("invalid metadata lock trustee"));
        }
        let matched = trustees
            .iter()
            .position(|(expected, mask)| {
                ace.Mask == *mask && unsafe { EqualSid(sid, expected.as_ptr() as PSID) } != 0
            })
            .ok_or_else(|| anyhow!("untrusted metadata lock trustee or rights"))?;
        if seen[matched] {
            return Err(anyhow!("duplicate metadata lock trustee"));
        }
        seen[matched] = true;
    }
    Ok(())
}

impl GlobalAclLock {
    pub fn acquire() -> Result<Self> {
        let handle = open_lock()?;
        match unsafe { WaitForSingleObject(handle.raw(), WAIT_MS) } {
            WAIT_OBJECT_0 | WAIT_ABANDONED => Ok(Self { handle, _thread: std::marker::PhantomData }),
            WAIT_TIMEOUT => Err(anyhow!("another metadata mutation holds the global lock; timed out without changing permissions")),
            _ => Err(winutil::last_error("WaitForSingleObject(metadata lock)")),
        }
    }
}

impl Drop for GlobalAclLock {
    fn drop(&mut self) {
        unsafe {
            ReleaseMutex(self.handle.raw());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{acl, identity, protocol, windows};
    use std::fs;
    use std::process::{Command, Stdio};
    use std::sync::atomic::AtomicBool;
    use windows_sys::Win32::Foundation::ERROR_ACCESS_DENIED;
    use windows_sys::Win32::Storage::FileSystem::{WRITE_DAC, WRITE_OWNER};
    use windows_sys::Win32::System::Threading::{CreateEventW, OpenMutexW};

    #[test]
    fn lock_creator_cannot_rewrite_permissions_and_nested_guards_work() {
        let _outer = GlobalAclLock::acquire().unwrap();
        let _inner = GlobalAclLock::acquire().unwrap();
        for rights in [WRITE_DAC, WRITE_OWNER] {
            let raw = unsafe { OpenMutexW(rights, 0, wide(NAME).as_ptr()) };
            let error = unsafe { GetLastError() };
            assert_eq!(
                raw, 0,
                "creator obtained administrative mutex access {rights:#x}"
            );
            assert_eq!(error, ERROR_ACCESS_DENIED);
        }
    }

    #[test]
    fn incompatible_precreated_lock_fails_closed() {
        let nonce = identity::random_profile_name().unwrap();
        let wrong_type = format!("Global\\{nonce}.WrongType");
        let _event = Handle::new(
            unsafe { CreateEventW(std::ptr::null(), 0, 0, wide(&wrong_type).as_ptr()) },
            "test event",
        )
        .unwrap();
        assert!(open_named_lock(&wrong_type).is_err());
        let wrong_acl = format!("Global\\{nonce}.WrongAcl");
        let _mutex = Handle::new(
            unsafe { CreateMutexExW(std::ptr::null(), wide(&wrong_acl).as_ptr(), 0, ACCESS) },
            "test mutex",
        )
        .unwrap();
        assert!(open_named_lock(&wrong_acl).is_err());
    }

    #[test]
    fn independent_acl_rmw_child() {
        let Some(root) = std::env::var_os("BELLO_TEST_ACL_RMW_ROOT") else {
            return;
        };
        let root = std::path::PathBuf::from(root);
        let profile = std::env::var("BELLO_TEST_ACL_RMW_PROFILE").unwrap();
        let sid = identity::derive_profile_sid(&profile).unwrap();
        let handle = winutil::open_path(&root, true).unwrap();
        for _ in 0..12 {
            acl::grant_object(&handle, sid.0, protocol::SandboxMode::ReadOnly, false).unwrap();
            std::thread::yield_now();
            acl::revoke(&handle, sid.0).unwrap();
            std::thread::yield_now();
        }
        acl::grant_object(&handle, sid.0, protocol::SandboxMode::ReadOnly, false).unwrap();
    }

    #[test]
    fn independent_process_acl_updates_preserve_all_peer_sids() {
        let profiles = (0..4)
            .map(|_| identity::random_profile_name().unwrap())
            .collect::<Vec<_>>();
        let root = std::env::temp_dir().join(format!("bello-global-rmw-{}", profiles[0]));
        fs::create_dir(&root).unwrap();
        let root = fs::canonicalize(root).unwrap();
        let handle = winutil::open_path(&root, true).unwrap();
        let before = acl::tests::paired_dacl_snapshot(&handle).unwrap();
        let children = profiles
            .iter()
            .map(|profile| {
                Command::new(std::env::current_exe().unwrap())
                    .args([
                        "--exact",
                        "global_acl_lock::tests::independent_acl_rmw_child",
                        "--nocapture",
                    ])
                    .env("BELLO_TEST_ACL_RMW_ROOT", &root)
                    .env("BELLO_TEST_ACL_RMW_PROFILE", profile)
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped())
                    .spawn()
                    .unwrap()
            })
            .collect::<Vec<_>>();
        for child in children {
            let output = child.wait_with_output().unwrap();
            assert!(output.status.success(), "RMW child failed: {output:?}");
        }
        for profile in &profiles {
            let sid = identity::derive_profile_sid(profile).unwrap();
            acl::verify_tree(
                &root,
                &[],
                sid.0,
                protocol::SandboxMode::ReadOnly,
                &std::sync::Arc::new(AtomicBool::new(false)),
            )
            .unwrap();
        }
        for profile in &profiles {
            let sid = identity::derive_profile_sid(profile).unwrap();
            acl::revoke(&handle, sid.0).unwrap();
        }
        assert_eq!(acl::tests::paired_dacl_snapshot(&handle).unwrap(), before);
        drop(handle);
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn lpac_mutex_access_child() {
        if std::env::var("BELLO_TEST_LPAC_MUTEX").as_deref() != Ok("1") {
            return;
        }
        let raw = unsafe { OpenMutexW(ACCESS, 0, wide(NAME).as_ptr()) };
        let error = unsafe { GetLastError() };
        assert_eq!(raw, 0, "LPAC opened host ACL mutation mutex");
        assert_eq!(
            error, ERROR_ACCESS_DENIED,
            "expected real access denial, not a missing mutex"
        );
    }

    #[test]
    fn actual_lpac_process_cannot_open_host_acl_mutex() {
        // Keep the real object present, but do not own/wait on it while the
        // sandbox command runs. Setup/cleanup must be able to take its lock.
        let _present = open_lock().unwrap();
        let root = std::env::temp_dir().join(format!(
            "bello-mutex-lpac-{}",
            identity::random_profile_name().unwrap()
        ));
        fs::create_dir(&root).unwrap();
        fs::copy(
            std::env::current_exe().unwrap(),
            root.join("mutex-probe.exe"),
        )
        .unwrap();
        let mut launcher = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "global_acl_lock::tests::lpac_launcher_child",
                "--nocapture",
            ])
            .env("BELLO_TEST_LPAC_ROOT", &root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        // Real production monitors controller EOF. Keep the write end alive;
        // wait_with_output would otherwise close Child.stdin before waiting.
        let keepalive = launcher.stdin.take().unwrap();
        let output = launcher.wait_with_output().unwrap();
        drop(keepalive);
        assert!(
            output.status.success(),
            "real LPAC launch failed: {output:?}"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn lpac_launcher_child() {
        let Some(root) = std::env::var_os("BELLO_TEST_LPAC_ROOT") else {
            return;
        };
        let path = fs::canonicalize(&root).unwrap();
        let path = path
            .to_str()
            .unwrap()
            .strip_prefix(r"\\?\")
            .unwrap()
            .to_owned();
        let request = protocol::Request::Run {
            protocol_version: protocol::PROTOCOL_VERSION,
            command: "set BELLO_TEST_LPAC_MUTEX=1&& mutex-probe.exe --exact global_acl_lock::tests::lpac_mutex_access_child --nocapture".to_owned(),
            cwd: path.clone(), root: path,
            mode: protocol::SandboxMode::WorkspaceWrite,
            readable_roots: Vec::new(), private_paths: Vec::new(), network_access: false,
        };
        let result = windows::execute(request);
        assert_eq!(result.unwrap(), 0, "real LPAC mutex probe failed");
    }
}
