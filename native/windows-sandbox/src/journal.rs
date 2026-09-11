use crate::acl;
use crate::identity::{delete_profile, derive_profile_sid, validate_profile_name};
use crate::winutil::{
    contains, file_identity, is_normalized_local_absolute, open_path, open_regular_file_read,
    path_eq, pin_directory_chain, require_persistent_acls, validate_final_path,
    validate_plain_file_object, wide, wide_ptr_to_os_string, FileIdentity, Handle,
};
use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use std::ffi::c_void;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::mem;
use std::path::{Path, PathBuf};
use windows_sys::Win32::Foundation::{
    GetLastError, ERROR_ALREADY_EXISTS, ERROR_FILE_NOT_FOUND, ERROR_INSUFFICIENT_BUFFER,
};
use windows_sys::Win32::Security::{
    AddAccessAllowedAce, GetLengthSid, GetTokenInformation, InitializeAcl,
    InitializeSecurityDescriptor, IsValidSid, SetSecurityDescriptorDacl,
    SetSecurityDescriptorOwner, TokenUser, ACCESS_ALLOWED_ACE, ACL, ACL_REVISION,
    SECURITY_ATTRIBUTES, SECURITY_DESCRIPTOR, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::Storage::FileSystem::{
    MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
};
use windows_sys::Win32::System::Com::CoTaskMemFree;
use windows_sys::Win32::System::Threading::{
    CreateMutexW, GetCurrentProcess, OpenMutexW, OpenProcessToken, SYNCHRONIZATION_SYNCHRONIZE,
};
use windows_sys::Win32::UI::Shell::{FOLDERID_LocalAppData, SHGetKnownFolderPath};

const JOURNAL_VERSION: u32 = 4;
const MAX_JOURNAL_BYTES: u64 = 4 * 1024 * 1024;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RecordedPath {
    path: PathBuf,
    identity: FileIdentity,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct JournalData {
    journal_version: u32,
    profile_name: String,
    mutex_name: String,
    authorities: Vec<RecordedPath>,
    touched_paths: Vec<RecordedPath>,
    created_paths: Vec<RecordedPath>,
    // v3 never granted outside its authorities. Default permits safe v3 cleanup,
    // but a v3 record with non-empty metadata_paths is explicitly rejected.
    #[serde(default)]
    metadata_paths: Vec<RecordedPath>,
}

pub struct Journal {
    path: PathBuf,
    data: JournalData,
}

pub struct StateDirectory {
    path: PathBuf,
    _ancestry_handles: Vec<Handle>,
}

fn open_plain_state_leaf(path: &Path) -> Result<Handle> {
    let handle = open_path(path, true)?;
    validate_plain_file_object(&handle, path)?;
    Ok(handle)
}

impl StateDirectory {
    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Journal {
    pub fn create(
        state_dir: &Path,
        profile_name: &str,
        mutex_name: &str,
        authorities: &[PathBuf],
    ) -> Result<Self> {
        validate_profile_name(profile_name)?;
        if authorities.is_empty() {
            return Err(anyhow!(
                "a recovery journal requires at least one authority"
            ));
        }
        let authorities = authorities
            .iter()
            .map(|path| record_existing_path(path))
            .collect::<Result<Vec<_>>>()?;
        fs::create_dir_all(state_dir).with_context(|| {
            format!(
                "could not create recovery directory {}",
                state_dir.display()
            )
        })?;
        let path = state_dir.join(format!("{profile_name}.json"));
        if path.exists() {
            return Err(anyhow!("recovery journal identity collision"));
        }
        let journal = Self {
            path,
            data: JournalData {
                journal_version: JOURNAL_VERSION,
                profile_name: profile_name.to_owned(),
                mutex_name: mutex_name.to_owned(),
                authorities,
                touched_paths: Vec::new(),
                created_paths: Vec::new(),
                metadata_paths: Vec::new(),
            },
        };
        validate_journal(&journal.data)?;
        journal.persist()?;
        Ok(journal)
    }

    pub fn profile_name(&self) -> &str {
        &self.data.profile_name
    }

    pub fn before_acl_mutation(&mut self, path: &Path, handle: &Handle) -> Result<()> {
        if !self
            .data
            .touched_paths
            .iter()
            .any(|existing| path_eq(&existing.path, path))
        {
            self.data.touched_paths.push(RecordedPath {
                path: path.to_owned(),
                identity: file_identity(handle)?,
            });
            self.persist()?;
        }
        Ok(())
    }

    pub fn record_created(&mut self, path: PathBuf, identity: FileIdentity) -> Result<()> {
        self.data
            .created_paths
            .push(RecordedPath { path, identity });
        self.persist()
    }

    pub fn before_metadata_mutation(&mut self, path: &Path, handle: &Handle) -> Result<()> {
        if !self
            .data
            .metadata_paths
            .iter()
            .any(|record| path_eq(&record.path, path))
        {
            self.data.metadata_paths.push(RecordedPath {
                path: path.to_owned(),
                identity: file_identity(handle)?,
            });
            validate_journal(&self.data)?;
            self.persist()?;
        }
        Ok(())
    }

    pub fn cleanup(self) -> Result<()> {
        let result = cleanup_data(&self.data);
        if result.is_ok() {
            // State-directory DACL propagation uses this same global lock.
            // Hold it only for this journal unlink, never the command or tree cleanup.
            let _acl_lock = crate::global_acl_lock::GlobalAclLock::acquire()?;
            fs::remove_file(&self.path).with_context(|| {
                format!("could not remove completed journal {}", self.path.display())
            })?;
        }
        result
    }

    pub fn defer_cleanup(self) {
        // Keep the durable journal/profile/ACEs for the next helper startup.
        // This is required when the current helper cannot prove its Job Object
        // is empty; revoking ACLs in that state would reopen a host escape.
    }

    fn persist(&self) -> Result<()> {
        let bytes = serde_json::to_vec(&self.data)?;
        if bytes.len() as u64 > MAX_JOURNAL_BYTES {
            return Err(anyhow!("Windows sandbox recovery journal exceeded 4 MiB"));
        }
        // Another helper protects the state directory before acquiring the
        // account lock. Its inheritable DACL update may open journal children;
        // serialize our complete temp-write/replace/cleanup window with that
        // update. The guard never outlives persistence or covers model commands.
        let _acl_lock = crate::global_acl_lock::GlobalAclLock::acquire()?;
        let temporary = self.path.with_extension("json.new");
        let mut file = OpenOptions::new()
            .create_new(true)
            .truncate(true)
            .write(true)
            .open(&temporary)
            .with_context(|| format!("could not write journal {}", temporary.display()))?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        drop(file);
        let replaced = unsafe {
            MoveFileExW(
                wide(&temporary).as_ptr(),
                wide(&self.path).as_ptr(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            )
        };
        if replaced == 0 {
            // Cleanup can overwrite the thread's last-error value. Capture the
            // failed replacement before doing any further filesystem work.
            let error =
                crate::winutil::last_error(&format!("MoveFileExW({})", self.path.display()));
            let _ = fs::remove_file(&temporary);
            return Err(error);
        }
        Ok(())
    }
}

pub fn state_directory() -> Result<StateDirectory> {
    let mut raw: *mut u16 = std::ptr::null_mut();
    let hr = unsafe { SHGetKnownFolderPath(&FOLDERID_LocalAppData, 0, 0, &mut raw) };
    if hr < 0 || raw.is_null() {
        return Err(anyhow!(
            "SHGetKnownFolderPath(LocalAppData) failed with HRESULT 0x{:08x}",
            hr as u32
        ));
    }
    let lexical_local = PathBuf::from(unsafe { wide_ptr_to_os_string(raw) });
    unsafe {
        CoTaskMemFree(raw as *const std::ffi::c_void);
    }
    if !is_normalized_local_absolute(&lexical_local) || !lexical_local.is_dir() {
        return Err(anyhow!(
            "the LocalAppData known folder is not an ordinary absolute local directory"
        ));
    }

    // Inspect and retain every lexical ancestor before canonicalization so a
    // junction in the known-folder path cannot be hidden by path resolution.
    let mut handles = Vec::new();
    let mut ancestry: Vec<&Path> = lexical_local.ancestors().collect();
    ancestry.reverse();
    for path in ancestry {
        if !path.is_absolute() || !path.exists() {
            continue;
        }
        let handle = open_path(path, false)?;
        let info = validate_plain_file_object(&handle, path)?;
        if info.file_index == 0 {
            return Err(anyhow!(
                "the Windows sandbox state ancestry has no stable identity: {}",
                path.display()
            ));
        }
        handles.push(handle);
    }

    let local = fs::canonicalize(&lexical_local).with_context(|| {
        format!(
            "could not canonicalize LocalAppData {}",
            lexical_local.display()
        )
    })?;
    let bello = local.join("Bello");
    if !bello.exists() {
        fs::create_dir(&bello)
            .or_else(|error| {
                if error.kind() == std::io::ErrorKind::AlreadyExists {
                    Ok(())
                } else {
                    Err(error)
                }
            })
            .with_context(|| format!("could not create state parent {}", bello.display()))?;
    }
    let bello_handle = open_path(&bello, false)?;
    validate_plain_file_object(&bello_handle, &bello)?;
    handles.push(bello_handle);

    let state = bello.join("SandboxState-v1");
    if !state.exists() {
        acl::create_state_directory(&state)
            .or_else(|error| {
                // A competing helper may have atomically created this leaf.
                // Reopen and perform the full no-follow/owner validation below.
                if error
                    .downcast_ref::<std::io::Error>()
                    .and_then(std::io::Error::raw_os_error)
                    == Some(183)
                {
                    Ok(())
                } else {
                    Err(error)
                }
            })
            .with_context(|| format!("could not create recovery directory {}", state.display()))?;
    }
    // Open the lexical leaf with OPEN_REPARSE_POINT before canonicalizing it.
    // Canonicalizing first would silently follow a pre-planted junction and
    // apply the protected recovery DACL to its unrelated target.
    let state_handle = open_plain_state_leaf(&state)?;
    let state = fs::canonicalize(&state).with_context(|| {
        format!(
            "could not canonicalize recovery directory {}",
            state.display()
        )
    })?;
    validate_final_path(&state_handle, &state)?;
    require_persistent_acls(&state_handle, &state)?;
    acl::protect_state_directory(&state_handle)?;
    handles.push(state_handle);
    Ok(StateDirectory {
        path: state,
        _ancestry_handles: handles,
    })
}

pub fn mutex_name(profile_name: &str) -> String {
    format!("Global\\{profile_name}")
}

fn account_sid_buffer() -> Result<Vec<usize>> {
    let mut token = 0;
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return Err(crate::winutil::last_error("OpenProcessToken(live marker)"));
    }
    let token = Handle::new(token, "OpenProcessToken(live marker)")?;
    let mut required = 0;
    let queried = unsafe {
        GetTokenInformation(
            token.raw(),
            TokenUser,
            std::ptr::null_mut(),
            0,
            &mut required,
        )
    };
    let code = unsafe { GetLastError() };
    if queried != 0
        || code != ERROR_INSUFFICIENT_BUFFER
        || required < mem::size_of::<TOKEN_USER>() as u32
        || required > 16 * 1024
    {
        return Err(anyhow!(
            "invalid live-marker account SID query (Win32 error {code})"
        ));
    }
    let mut buffer = vec![0_usize; (required as usize).div_ceil(mem::size_of::<usize>())];
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
        return Err(crate::winutil::last_error(
            "GetTokenInformation(live-marker account)",
        ));
    }
    let sid = unsafe { (*(buffer.as_ptr() as *const TOKEN_USER)).User.Sid };
    if sid.is_null() || unsafe { IsValidSid(sid) } == 0 {
        return Err(anyhow!("invalid live-marker account SID"));
    }
    Ok(buffer)
}

pub fn create_live_mutex(name: &str) -> Result<Handle> {
    validate_profile_name(
        name.strip_prefix("Global\\")
            .ok_or_else(|| anyhow!("live marker must use the global namespace"))?,
    )?;
    // A per-run global object is visible from every session of this account.
    // It is only held open, never acquired/waited on: commands remain parallel.
    // Grant the account SID, not the session's logon SID, and no AppContainer SID.
    let user = account_sid_buffer()?;
    let sid = unsafe { (*(user.as_ptr() as *const TOKEN_USER)).User.Sid };
    let acl_bytes = mem::size_of::<ACL>() + mem::size_of::<ACCESS_ALLOWED_ACE>()
        - mem::size_of::<u32>()
        + unsafe { GetLengthSid(sid) } as usize;
    let mut acl_storage = vec![0_usize; acl_bytes.div_ceil(mem::size_of::<usize>())];
    let dacl = acl_storage.as_mut_ptr() as *mut ACL;
    let mut descriptor: SECURITY_DESCRIPTOR = unsafe { mem::zeroed() };
    let descriptor_ptr = &mut descriptor as *mut _ as *mut c_void;
    if unsafe { InitializeAcl(dacl, acl_bytes as u32, ACL_REVISION) } == 0
        || unsafe { AddAccessAllowedAce(dacl, ACL_REVISION, SYNCHRONIZATION_SYNCHRONIZE, sid) } == 0
        || unsafe { InitializeSecurityDescriptor(descriptor_ptr, 1) } == 0
        || unsafe { SetSecurityDescriptorOwner(descriptor_ptr, sid, 0) } == 0
        || unsafe { SetSecurityDescriptorDacl(descriptor_ptr, 1, dacl, 0) } == 0
    {
        return Err(crate::winutil::last_error(
            "initialize live-marker security",
        ));
    }
    let attributes = SECURITY_ATTRIBUTES {
        nLength: mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor_ptr,
        bInheritHandle: 0,
    };
    let raw = unsafe { CreateMutexW(&attributes, 0, wide(name).as_ptr()) };
    let code = unsafe { GetLastError() };
    if raw == 0 {
        return Err(anyhow!(
            "CreateMutexW(live marker) failed with Win32 error {code}"
        ));
    }
    let handle = Handle::new(raw, "CreateMutexW(live marker)")?;
    if code == ERROR_ALREADY_EXISTS {
        return Err(anyhow!(
            "live-marker identity collision; refusing an existing mutex"
        ));
    }
    Ok(handle)
}

fn mutex_is_live(name: &str) -> Result<bool> {
    let handle = unsafe { OpenMutexW(SYNCHRONIZATION_SYNCHRONIZE, 0, wide(name).as_ptr()) };
    if handle == 0 {
        let code = unsafe { GetLastError() };
        if code == ERROR_FILE_NOT_FOUND {
            Ok(false)
        } else {
            Err(anyhow!("could not establish sandbox liveness (Win32 error {code}); recovery journal retained"))
        }
    } else {
        drop(Handle::new(handle, "OpenMutexW(live marker)")?);
        Ok(true)
    }
}

pub fn recover_stale(state_dir: &Path) -> Result<()> {
    if !state_dir.exists() {
        return Ok(());
    }
    for entry in fs::read_dir(state_dir)
        .with_context(|| format!("could not enumerate {}", state_dir.display()))?
    {
        let entry = entry?;
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let file_name = path
            .file_stem()
            .and_then(|value| value.to_str())
            .ok_or_else(|| anyhow!("invalid recovery journal name"))?;
        validate_profile_name(file_name)?;
        let mut file = open_regular_file_read(&path)?;
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.len() > MAX_JOURNAL_BYTES {
            return Err(anyhow!("invalid recovery journal {}", path.display()));
        }
        let mut bytes = Vec::with_capacity(metadata.len() as usize);
        Read::by_ref(&mut file)
            .take(MAX_JOURNAL_BYTES + 1)
            .read_to_end(&mut bytes)
            .with_context(|| format!("could not read {}", path.display()))?;
        drop(file);
        if bytes.len() as u64 > MAX_JOURNAL_BYTES {
            return Err(anyhow!("oversized recovery journal {}", path.display()));
        }
        let data: JournalData = serde_json::from_slice(&bytes)
            .with_context(|| format!("invalid recovery journal {}", path.display()))?;
        validate_journal(&data)?;
        if data.profile_name != file_name {
            return Err(anyhow!(
                "recovery journal filename does not match its profile identity"
            ));
        }
        if mutex_is_live(&data.mutex_name)? {
            continue;
        }
        cleanup_data(&data)
            .with_context(|| format!("could not recover stale sandbox {}", data.profile_name))?;
        let _acl_lock = crate::global_acl_lock::GlobalAclLock::acquire()?;
        fs::remove_file(&path)
            .with_context(|| format!("could not remove recovered journal {}", path.display()))?;
    }
    Ok(())
}

fn validate_journal(data: &JournalData) -> Result<()> {
    validate_profile_name(&data.profile_name)?;
    if data.journal_version == 2 && data.mutex_name == format!("Local\\{}", data.profile_name) {
        return Err(anyhow!("legacy session-local recovery journal retained: another Windows session may still own this run; automatic recovery cannot establish its liveness"));
    }
    if !matches!(data.journal_version, 3 | JOURNAL_VERSION)
        || (data.journal_version == 3 && !data.metadata_paths.is_empty())
    {
        return Err(anyhow!("unsupported recovery journal version"));
    }
    if data.mutex_name != mutex_name(&data.profile_name) {
        return Err(anyhow!("recovery journal mutex does not match its profile"));
    }
    if data.authorities.is_empty() {
        return Err(anyhow!("recovery journal contains no authorities"));
    }
    for record in &data.authorities {
        validate_recorded_path(record, "authority")?;
    }
    for record in data.touched_paths.iter().chain(data.created_paths.iter()) {
        validate_recorded_path(record, "mutated path")?;
        if !data
            .authorities
            .iter()
            .any(|authority| contains(&authority.path, &record.path))
        {
            return Err(anyhow!(
                "recovery journal path is outside every recorded authority: {}",
                record.path.display()
            ));
        }
    }
    let fixed = if data.metadata_paths.is_empty() {
        Vec::new()
    } else {
        crate::host_prepare::fixed_targets()?
    };
    for record in &data.metadata_paths {
        validate_recorded_path(record, "metadata ancestor")?;
        if crate::winutil::is_volume_root(&record.path)
            || fixed.iter().any(|target| {
                crate::winutil::verbatim_local_absolute(&target.path)
                    .is_ok_and(|path| path_eq(&path, &record.path))
            })
            || data
                .authorities
                .iter()
                .any(|authority| contains(&authority.path, &record.path))
            || !data
                .authorities
                .iter()
                .any(|authority| contains(&record.path, &authority.path))
        {
            return Err(anyhow!("metadata journal path must be a proper ancestor outside all authorities and fixed host targets: {}", record.path.display()));
        }
    }
    Ok(())
}

fn validate_recorded_path(record: &RecordedPath, label: &str) -> Result<()> {
    if !is_normalized_local_absolute(&record.path) || record.identity.file_index == 0 {
        return Err(anyhow!("recovery journal contains an invalid {label}"));
    }
    Ok(())
}

fn record_existing_path(path: &Path) -> Result<RecordedPath> {
    let handle = open_path(path, false)?;
    validate_final_path(&handle, path)?;
    validate_plain_file_object(&handle, path)?;
    require_persistent_acls(&handle, path)?;
    Ok(RecordedPath {
        path: path.to_owned(),
        identity: file_identity(&handle)?,
    })
}

fn reopen_recorded(record: &RecordedPath, write_dac: bool) -> Result<Handle> {
    let handle = open_path(&record.path, write_dac)?;
    validate_final_path(&handle, &record.path)?;
    validate_plain_file_object(&handle, &record.path)?;
    let identity = file_identity(&handle)?;
    if identity.volume_serial != record.identity.volume_serial
        || identity.file_index != record.identity.file_index
    {
        return Err(anyhow!(
            "recovery path identity changed: {}",
            record.path.display()
        ));
    }
    Ok(handle)
}

fn cleanup_data(data: &JournalData) -> Result<()> {
    // These are exact outside objects, NOT recursive cleanup authorities. Pin
    // their full lexical ancestry and match identity before opening WRITE_DAC.
    let metadata_pins = data
        .metadata_paths
        .iter()
        .map(|record| {
            let pins = pin_directory_chain(&record.path, false)?;
            let (_, handle) = pins
                .last()
                .ok_or_else(|| anyhow!("empty metadata ancestry"))?;
            let identity = file_identity(handle)?;
            if identity.volume_serial != record.identity.volume_serial
                || identity.file_index != record.identity.file_index
            {
                return Err(anyhow!(
                    "metadata recovery path identity changed: {}",
                    record.path.display()
                ));
            }
            Ok(pins)
        })
        .collect::<Result<Vec<_>>>()?;
    let _authority_handles = data
        .authorities
        .iter()
        .map(|record| reopen_recorded(record, false))
        .collect::<Result<Vec<_>>>()?;
    let sid = derive_profile_sid(&data.profile_name)?;
    let mut failures = Vec::new();
    // The authority identities are the durable write-ahead anchors. Ordinary
    // objects may legitimately be new, renamed, or deleted; never reopen their
    // historical names. Traverse the current pinned tree and remove only this
    // run's SID, including explicit ACEs on objects created by the child.
    for record in &data.authorities {
        if let Err(error) = crate::winutil::walk_pinned_tree(
            &record.path,
            true,
            &std::sync::atomic::AtomicBool::new(false),
            &mut |path, handle| {
                acl::revoke(handle, sid.0).with_context(|| format!("revoke {}", path.display()))
            },
        ) {
            failures.push(format!(
                "revoke authority {}: {error:#}",
                record.path.display()
            ));
        }
    }
    for (record, pins) in data.metadata_paths.iter().zip(&metadata_pins) {
        let result = (|| -> Result<()> {
            let pinned = &pins
                .last()
                .ok_or_else(|| anyhow!("empty metadata ancestry"))?
                .1;
            let mutation = crate::host_prepare::metadata_mutation_lock(&record.path, pinned)?;
            acl::revoke(&mutation.writable, sid.0)?;
            acl::verify_absent_object(&mutation.writable, sid.0)
        })();
        if let Err(error) = result {
            failures.push(format!(
                "revoke exact metadata ancestor {}: {error:#}",
                record.path.display()
            ));
        }
    }
    if !failures.is_empty() {
        return Err(anyhow!(failures.join("; ")));
    }
    for record in &data.authorities {
        if let Err(error) = acl::verify_absent_tree(&record.path, sid.0) {
            failures.push(format!(
                "verify AppContainer ACE removal under {}: {error:#}",
                record.path.display()
            ));
        }
    }
    if !failures.is_empty() {
        return Err(anyhow!(failures.join("; ")));
    }
    if let Err(error) = delete_profile(&data.profile_name) {
        return Err(anyhow!("delete profile: {error:#}"));
    }
    // The per-object traversal pins are now released; authority pins remain.
    for created in data.created_paths.iter().rev() {
        match open_path(&created.path, false).and_then(|handle| file_identity(&handle)) {
            Ok(identity)
                if identity.volume_serial == created.identity.volume_serial
                    && identity.file_index == created.identity.file_index =>
            {
                if let Err(error) = fs::remove_dir(&created.path) {
                    // ERROR_DIR_NOT_EMPTY. Avoid ErrorKind::DirectoryNotEmpty,
                    // which postdates this crate's declared Rust 1.75 MSRV.
                    // A live peer may pin an otherwise empty private directory.
                    // It is not ours to force-delete; removing our SID above
                    // has already been verified across the current tree.
                    if !matches!(error.raw_os_error(), Some(145 | 32)) {
                        failures.push(format!("remove {}: {error}", created.path.display()));
                    }
                }
            }
            Ok(_) => {}
            Err(error) if error.to_string().contains("Win32 error 2") => {}
            Err(_) if !created.path.exists() => {}
            Err(error) => failures.push(format!("inspect {}: {error:#}", created.path.display())),
        }
    }
    if failures.is_empty() {
        Ok(())
    } else {
        Err(anyhow!(failures.join("; ")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::{Command, Stdio};
    use std::time::{SystemTime, UNIX_EPOCH};
    use windows_sys::Win32::Foundation::{GetHandleInformation, HANDLE_FLAG_INHERIT};
    use windows_sys::Win32::Security::{
        EqualSid, GetAce, GetKernelObjectSecurity, GetSecurityDescriptorDacl,
        GetSecurityDescriptorOwner, SetKernelObjectSecurity, DACL_SECURITY_INFORMATION,
        OWNER_SECURITY_INFORMATION,
    };
    use windows_sys::Win32::System::Threading::CreateEventW;

    #[test]
    fn live_marker_child_probe() {
        let Ok(name) = std::env::var("BELLO_TEST_LIVE_MARKER") else {
            return;
        };
        assert!(
            mutex_is_live(&name).unwrap(),
            "another helper cannot see the live marker"
        );
    }

    #[test]
    fn global_live_marker_has_account_only_access_and_is_not_inherited() {
        let profile = crate::identity::random_profile_name().unwrap();
        let name = mutex_name(&profile);
        assert!(name.starts_with("Global\\"));
        assert!(!mutex_is_live(&name).unwrap());
        let marker = create_live_mutex(&name).unwrap();
        assert!(mutex_is_live(&name).unwrap());
        let mut flags = 0;
        assert_ne!(unsafe { GetHandleInformation(marker.raw(), &mut flags) }, 0);
        assert_eq!(flags & HANDLE_FLAG_INHERIT, 0);

        let mut required = 0;
        let information = OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION;
        assert_eq!(
            unsafe {
                GetKernelObjectSecurity(
                    marker.raw(),
                    information,
                    std::ptr::null_mut(),
                    0,
                    &mut required,
                )
            },
            0
        );
        assert_eq!(unsafe { GetLastError() }, ERROR_INSUFFICIENT_BUFFER);
        let mut descriptor = vec![0_usize; (required as usize).div_ceil(mem::size_of::<usize>())];
        let sd = descriptor.as_mut_ptr() as *mut c_void;
        assert_ne!(
            unsafe {
                GetKernelObjectSecurity(marker.raw(), information, sd, required, &mut required)
            },
            0
        );
        let mut dacl: *mut ACL = std::ptr::null_mut();
        let mut present = 0;
        let mut defaulted = 0;
        assert_ne!(
            unsafe { GetSecurityDescriptorDacl(sd, &mut present, &mut dacl, &mut defaulted) },
            0
        );
        assert_ne!(present, 0);
        assert!(!dacl.is_null());
        assert_eq!(unsafe { (*dacl).AceCount }, 1);
        let mut ace = std::ptr::null_mut();
        assert_ne!(unsafe { GetAce(dacl, 0, &mut ace) }, 0);
        let ace = unsafe { &*(ace as *const ACCESS_ALLOWED_ACE) };
        assert_eq!(ace.Header.AceType, 0);
        assert_eq!(ace.Header.AceFlags, 0);
        assert_eq!(ace.Mask, SYNCHRONIZATION_SYNCHRONIZE);
        let account = account_sid_buffer().unwrap();
        let account_sid = unsafe { (*(account.as_ptr() as *const TOKEN_USER)).User.Sid };
        assert_ne!(
            unsafe { EqualSid(account_sid, &ace.SidStart as *const _ as *mut c_void) },
            0
        );
        let mut owner = std::ptr::null_mut();
        assert_ne!(
            unsafe { GetSecurityDescriptorOwner(sd, &mut owner, &mut defaulted) },
            0
        );
        assert_ne!(unsafe { EqualSid(account_sid, owner) }, 0);
        assert!(
            create_live_mutex(&name).is_err(),
            "existing identity must not be adopted"
        );

        // An independently started process must open the account-authorized
        // object, not inherit our handle. This is not a two-session substitute.
        let output = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "journal::tests::live_marker_child_probe",
                "--nocapture",
            ])
            .env("BELLO_TEST_LIVE_MARKER", &name)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        drop(marker);
        assert!(!mutex_is_live(&name).unwrap());
    }

    fn liveness_fixture() -> (PathBuf, Journal) {
        let profile = crate::identity::random_profile_name().unwrap();
        let base = std::env::temp_dir().join(format!("bello-liveness-{profile}"));
        fs::create_dir(&base).unwrap();
        // Windows TEMP may use an 8.3 account alias. Production state_directory
        // supplies canonical paths; the fixture must obey the same contract.
        let base = fs::canonicalize(&base).unwrap();
        fs::create_dir(base.join("workspace")).unwrap();
        let root = fs::canonicalize(base.join("workspace")).unwrap();
        fs::write(root.join("keep.txt"), "unchanged").unwrap();
        let journal = Journal::create(
            &base.join("state"),
            &profile,
            &mutex_name(&profile),
            &[root],
        )
        .unwrap();
        (base, journal)
    }

    #[test]
    fn failed_journal_replace_preserves_original_error_and_complete_old_record() {
        use windows_sys::Win32::Foundation::{ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION};
        let (base, mut journal) = liveness_fixture();
        let before = fs::read(&journal.path).unwrap();
        // This intentionally refuses delete sharing, forcing a real native
        // replacement failure rather than an injected mock error.
        let held = open_regular_file_read(&journal.path).unwrap();
        let probe = journal.path.with_extension("json.probe");
        fs::write(&probe, b"not a journal").unwrap();
        let replaced = unsafe {
            MoveFileExW(
                wide(&probe).as_ptr(),
                wide(&journal.path).as_ptr(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            )
        };
        let original_code = unsafe { GetLastError() };
        assert_eq!(replaced, 0);
        assert!(
            matches!(original_code, ERROR_ACCESS_DENIED | ERROR_SHARING_VIOLATION),
            "unexpected native sharing denial {original_code}"
        );
        fs::remove_file(probe).unwrap();
        journal
            .data
            .touched_paths
            .push(record_existing_path(&base.join("workspace")).unwrap());
        let error = journal.persist().unwrap_err();
        assert!(
            error
                .to_string()
                .ends_with(&format!("Win32 error {original_code}")),
            "replacement error was lost during cleanup: {error:#}"
        );
        assert_eq!(fs::read(&journal.path).unwrap(), before);
        validate_journal(&serde_json::from_slice::<JournalData>(&before).unwrap()).unwrap();
        assert!(!journal.path.with_extension("json.new").exists());
        drop(held);
        journal.persist().unwrap();
        assert_eq!(
            fs::read(&journal.path).unwrap(),
            serde_json::to_vec(&journal.data).unwrap()
        );
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn concurrent_state_protection_and_persist_keep_complete_immutable_journals() {
        use std::sync::{mpsc, Arc, Barrier};
        const UPDATES: usize = 40;
        let profile = crate::identity::random_profile_name().unwrap();
        let base = std::env::temp_dir().join(format!("bello-journal-concurrency-{profile}"));
        acl::create_state_directory(&base).unwrap();
        let base = fs::canonicalize(base).unwrap();
        let state = base.join("state");
        acl::create_state_directory(&state).unwrap();
        let root = base.join("workspace");
        fs::create_dir(&root).unwrap();
        let records = (0..UPDATES)
            .map(|index| {
                let path = root.join(format!("evidence-{index}.txt"));
                fs::write(&path, format!("evidence-{index}")).unwrap();
                record_existing_path(&path).unwrap()
            })
            .collect::<Vec<_>>();
        let journal = Journal::create(
            &state,
            &profile,
            &mutex_name(&profile),
            std::slice::from_ref(&root),
        )
        .unwrap();
        let peer_profile = crate::identity::random_profile_name().unwrap();
        let peer = Journal::create(
            &state,
            &peer_profile,
            &mutex_name(&peer_profile),
            std::slice::from_ref(&root),
        )
        .unwrap();
        let immutable = fs::read(&peer.path).unwrap();
        let state_handle = open_path(&state, true).unwrap();
        acl::protect_state_directory(&state_handle).unwrap();
        let initial_acl = acl::tests::paired_dacl_snapshot(&state_handle).unwrap();
        let start = Arc::new(Barrier::new(3));
        let (updates, received) = mpsc::channel();
        let writer_start = Arc::clone(&start);
        let writer = std::thread::spawn(move || {
            let mut journal = journal;
            writer_start.wait();
            for (index, record) in records.into_iter().enumerate() {
                journal.data.touched_paths.push(record);
                journal.persist().unwrap();
                updates.send(index + 1).unwrap();
                std::thread::yield_now();
            }
            journal
        });
        let protector_start = Arc::clone(&start);
        let peer_path = peer.path.clone();
        let immutable_peer = immutable.clone();
        let protector = std::thread::spawn(move || {
            protector_start.wait();
            for _ in 0..UPDATES {
                // The actual production setter propagates its inheritable DACL.
                // It and persist must share the same short global guard.
                acl::protect_state_directory(&state_handle).unwrap();
                assert_eq!(fs::read(&peer_path).unwrap(), immutable_peer);
                std::thread::yield_now();
            }
        });
        start.wait();
        let path = state.join(format!("{profile}.json"));
        let mut observed = 0;
        for minimum in received {
            // No reader lock: atomic replacement must expose a whole old or new
            // record, never partially written JSON or unrelated record contents.
            let current: JournalData = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            validate_journal(&current).unwrap();
            assert_eq!(current.profile_name, profile);
            assert!((minimum..=UPDATES).contains(&current.touched_paths.len()));
            observed += 1;
        }
        let journal = writer.join().unwrap();
        protector.join().unwrap();
        assert_eq!(observed, UPDATES);
        assert_eq!(
            fs::read(&journal.path).unwrap(),
            serde_json::to_vec(&journal.data).unwrap()
        );
        assert_eq!(fs::read(&peer.path).unwrap(), immutable);
        assert!(!journal.path.with_extension("json.new").exists());
        assert_eq!(
            acl::tests::paired_dacl_snapshot(&open_path(&state, true).unwrap()).unwrap(),
            initial_acl
        );
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn metadata_recovery_is_exact_nonrecursive_and_preserves_peer_grants() {
        let profile = crate::identity::random_profile_name().unwrap();
        let base = std::env::temp_dir().join(format!("bello-metadata-recovery-{profile}"));
        acl::create_state_directory(&base).unwrap();
        let base = fs::canonicalize(base).unwrap();
        fs::create_dir(base.join("workspace")).unwrap();
        fs::create_dir(base.join("outside")).unwrap();
        fs::write(base.join("outside/keep.txt"), "outside survives").unwrap();
        let root = base.join("workspace");
        let mut journal = Journal::create(
            &base.join("state"),
            &profile,
            &mutex_name(&profile),
            &[root],
        )
        .unwrap();
        let ours = derive_profile_sid(&profile).unwrap();
        let peer = derive_profile_sid(&crate::identity::random_profile_name().unwrap()).unwrap();
        let parent = open_path(&base, true).unwrap();
        let outside = open_path(&base.join("outside"), true).unwrap();
        acl::set_system_root_metadata(&parent, peer.0, true).unwrap();
        let before = acl::tests::paired_dacl_snapshot(&parent).unwrap();
        let outside_before = acl::tests::paired_dacl_snapshot(&outside).unwrap();
        journal.before_metadata_mutation(&base, &parent).unwrap();
        acl::set_system_root_metadata(&parent, ours.0, true).unwrap();
        assert_eq!(
            acl::tests::paired_dacl_snapshot(&outside).unwrap(),
            outside_before
        );
        fs::create_dir(base.join("new-outside")).unwrap();
        acl::verify_absent_object(
            &open_path(&base.join("new-outside"), false).unwrap(),
            ours.0,
        )
        .unwrap();
        // A sentinel with the same SID outside all authorities must survive:
        // recovery may touch the recorded parent, never recursively its tree.
        acl::set_system_root_metadata(&outside, ours.0, true).unwrap();
        let saved_path = journal.path.clone();
        drop(journal);
        recover_stale(&base.join("state")).unwrap();
        assert!(!saved_path.exists());
        assert_eq!(acl::tests::paired_dacl_snapshot(&parent).unwrap(), before);
        assert!(acl::system_root_metadata_prepared(&outside, ours.0).unwrap());
        assert_eq!(
            fs::read_to_string(base.join("outside/keep.txt")).unwrap(),
            "outside survives"
        );
        acl::revoke(&outside, ours.0).unwrap();
        acl::revoke(&parent, peer.0).unwrap();
        drop((parent, outside));
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn metadata_recovery_refuses_replaced_ancestor_identity() {
        let (base, mut journal) = liveness_fixture();
        let ancestor = base.join("ancestor");
        acl::create_state_directory(&ancestor).unwrap();
        fs::rename(base.join("workspace"), ancestor.join("workspace")).unwrap();
        journal.data.authorities = vec![record_existing_path(&ancestor.join("workspace")).unwrap()];
        let ours = derive_profile_sid(journal.profile_name()).unwrap();
        let handle = open_path(&ancestor, true).unwrap();
        journal
            .before_metadata_mutation(&ancestor, &handle)
            .unwrap();
        acl::set_system_root_metadata(&handle, ours.0, true).unwrap();
        drop(handle);
        let before = fs::read(&journal.path).unwrap();
        let moved = base.join("original-ancestor");
        fs::rename(&ancestor, &moved).unwrap();
        fs::create_dir_all(ancestor.join("workspace")).unwrap();
        let error = recover_stale(&base.join("state")).unwrap_err();
        assert!(format!("{error:#}").contains("metadata recovery path identity changed"));
        assert_eq!(fs::read(&journal.path).unwrap(), before);
        acl::verify_absent_object(&open_path(&ancestor, false).unwrap(), ours.0).unwrap();
        acl::revoke(&open_path(&moved, true).unwrap(), ours.0).unwrap();
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn metadata_journal_rejects_scope_expansion_and_recovers_v3_without_metadata() {
        let (base, mut journal) = liveness_fixture();
        let original = serde_json::to_vec(&journal.data).unwrap();
        for invalid in [base.join("workspace"), base.join("state")] {
            journal.data.metadata_paths = vec![record_existing_path(&invalid).unwrap()];
            assert!(validate_journal(&journal.data).is_err());
        }
        journal.data.metadata_paths = vec![record_existing_path(&base).unwrap()];
        assert!(validate_journal(&journal.data).is_ok());
        journal.data.journal_version = 3;
        assert!(validate_journal(&journal.data).is_err());
        let mut old: serde_json::Value = serde_json::from_slice(&original).unwrap();
        old["journalVersion"] = 3.into();
        old.as_object_mut().unwrap().remove("metadataPaths");
        journal.data = serde_json::from_value(old).unwrap();
        journal.persist().unwrap();
        let path = journal.path.clone();
        drop(journal);
        recover_stale(&base.join("state")).unwrap();
        assert!(!path.exists());
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn recovery_retains_live_and_ambiguous_markers_but_recovers_absent_ones() {
        let (base, journal) = liveness_fixture();
        let name = &journal.data.mutex_name;
        let before = fs::read(&journal.path).unwrap();
        let marker = create_live_mutex(name).unwrap();
        recover_stale(&base.join("state")).unwrap();
        assert_eq!(fs::read(&journal.path).unwrap(), before);

        // Even the owning account has no implicit SYNCHRONIZE right. An
        // inaccessible marker is unknown, not a dead helper to recover.
        let mut empty_acl: ACL = unsafe { mem::zeroed() };
        assert_ne!(
            unsafe { InitializeAcl(&mut empty_acl, mem::size_of::<ACL>() as u32, ACL_REVISION) },
            0
        );
        let mut descriptor: SECURITY_DESCRIPTOR = unsafe { mem::zeroed() };
        let sd = &mut descriptor as *mut _ as *mut c_void;
        assert_ne!(unsafe { InitializeSecurityDescriptor(sd, 1) }, 0);
        assert_ne!(
            unsafe { SetSecurityDescriptorDacl(sd, 1, &empty_acl, 0) },
            0
        );
        assert_ne!(
            unsafe { SetKernelObjectSecurity(marker.raw(), DACL_SECURITY_INFORMATION, sd) },
            0
        );
        assert!(mutex_is_live(name)
            .unwrap_err()
            .to_string()
            .contains("Win32 error 5"));
        assert!(recover_stale(&base.join("state")).is_err());
        assert_eq!(fs::read(&journal.path).unwrap(), before);
        drop(marker);

        let event = Handle::new(
            unsafe { CreateEventW(std::ptr::null(), 0, 0, wide(name).as_ptr()) },
            "CreateEventW(liveness collision fixture)",
        )
        .unwrap();
        assert!(
            mutex_is_live(name).is_err(),
            "wrong object type is not an absent marker"
        );
        assert!(create_live_mutex(name).is_err());
        assert!(recover_stale(&base.join("state")).is_err());
        assert_eq!(fs::read(&journal.path).unwrap(), before);
        drop(event);

        recover_stale(&base.join("state")).unwrap();
        assert!(!journal.path.exists());
        assert_eq!(
            fs::read_to_string(base.join("workspace/keep.txt")).unwrap(),
            "unchanged"
        );
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn legacy_local_journal_is_retained_even_without_a_marker_in_this_session() {
        let (base, mut journal) = liveness_fixture();
        journal.data.journal_version = 2;
        journal.data.mutex_name = format!("Local\\{}", journal.data.profile_name);
        journal.persist().unwrap();
        let before = fs::read(&journal.path).unwrap();
        assert!(!mutex_is_live(&journal.data.mutex_name).unwrap());
        let error = recover_stale(&base.join("state")).unwrap_err();
        assert!(
            error.to_string().contains("legacy session-local"),
            "unexpected legacy recovery error: {error:#}"
        );
        assert_eq!(fs::read(&journal.path).unwrap(), before);
        assert_eq!(
            fs::read_to_string(base.join("workspace/keep.txt")).unwrap(),
            "unchanged"
        );
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn cleanup_follows_current_tree_after_rename_and_preserves_peer_grants() {
        use crate::protocol::SandboxMode;
        use std::sync::atomic::AtomicBool;
        let base = std::env::temp_dir().join(format!(
            "bello-cleanup-current-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
        ));
        fs::create_dir_all(base.join("workspace").join("ordinary")).unwrap();
        let root = fs::canonicalize(base.join("workspace")).unwrap();
        fs::write(root.join("ordinary").join("existing.txt"), "existing").unwrap();
        let profile = crate::identity::random_profile_name().unwrap();
        let peer_profile = crate::identity::random_profile_name().unwrap();
        let ours = derive_profile_sid(&profile).unwrap();
        let peer = derive_profile_sid(&peer_profile).unwrap();
        let mut journal = Journal::create(
            &base.join("state"),
            &profile,
            &mutex_name(&profile),
            std::slice::from_ref(&root),
        )
        .unwrap();
        journal
            .before_acl_mutation(&root, &open_path(&root, true).unwrap())
            .unwrap();
        crate::winutil::walk_pinned_tree(
            &root,
            true,
            &AtomicBool::new(false),
            &mut |path, handle| {
                acl::grant_object(
                    handle,
                    ours.0,
                    SandboxMode::WorkspaceWrite,
                    !path_eq(&root, path),
                )?;
                acl::grant_object(handle, peer.0, SandboxMode::ReadOnly, false)
            },
        )
        .unwrap();
        fs::rename(root.join("ordinary"), root.join("renamed")).unwrap();
        fs::create_dir(root.join("new")).unwrap();
        fs::write(root.join("new").join("created.txt"), "new").unwrap();
        journal.cleanup().unwrap();
        acl::verify_absent_tree(&root, ours.0).unwrap();
        acl::verify_tree(
            &root,
            &[],
            peer.0,
            SandboxMode::ReadOnly,
            &std::sync::Arc::new(AtomicBool::new(false)),
        )
        .unwrap();
        assert_eq!(
            fs::read_to_string(root.join("renamed").join("existing.txt")).unwrap(),
            "existing"
        );
        assert_eq!(
            fs::read_to_string(root.join("new").join("created.txt")).unwrap(),
            "new"
        );
        crate::winutil::walk_pinned_tree(&root, true, &AtomicBool::new(false), &mut |_, handle| {
            acl::revoke(handle, peer.0)
        })
        .unwrap();
        acl::verify_absent_tree(&root, peer.0).unwrap();
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn state_leaf_junction_is_rejected_without_touching_target_acl() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let base = std::env::temp_dir().join(format!(
            "bello-state-junction-{}-{nonce}",
            std::process::id()
        ));
        let parent = base.join("parent");
        let target = base.join("unrelated-target");
        let junction = parent.join("SandboxState-v1");
        fs::create_dir_all(&parent).unwrap();
        fs::create_dir_all(&target).unwrap();
        let before = Command::new("icacls.exe").arg(&target).output().unwrap();
        assert!(before.status.success());
        let linked = Command::new("cmd.exe")
            .args(["/d", "/s", "/c", "mklink", "/J"])
            .arg(&junction)
            .arg(&target)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .unwrap();
        assert!(linked.success(), "could not create junction test fixture");

        let error = open_plain_state_leaf(&junction).unwrap_err();
        assert!(error.to_string().contains("reparse point"));
        let after = Command::new("icacls.exe").arg(&target).output().unwrap();
        assert!(after.status.success());
        assert_eq!(before.stdout, after.stdout, "junction target ACL changed");

        fs::remove_dir(&junction).unwrap();
        fs::remove_dir_all(&base).unwrap();
    }
}
