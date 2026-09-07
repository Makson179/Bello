use crate::acl;
use crate::identity::{delete_profile, derive_profile_sid, validate_profile_name};
use crate::winutil::{
    contains, file_identity, is_normalized_local_absolute, open_path, open_regular_file_read,
    path_eq, require_persistent_acls, validate_final_path, validate_plain_file_object, wide,
    wide_ptr_to_os_string, FileIdentity, Handle,
};
use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use windows_sys::Win32::Foundation::CloseHandle;
use windows_sys::Win32::Storage::FileSystem::{
    MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
};
use windows_sys::Win32::System::Com::CoTaskMemFree;
use windows_sys::Win32::System::Threading::{
    CreateMutexW, OpenMutexW, SYNCHRONIZATION_SYNCHRONIZE,
};
use windows_sys::Win32::UI::Shell::{FOLDERID_LocalAppData, SHGetKnownFolderPath};

const JOURNAL_VERSION: u32 = 2;
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
            },
        };
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

    pub fn cleanup(self) -> Result<()> {
        let result = cleanup_data(&self.data);
        if result.is_ok() {
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
            let _ = fs::remove_file(&temporary);
            return Err(crate::winutil::last_error(&format!(
                "MoveFileExW({})",
                self.path.display()
            )));
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
            .with_context(|| format!("could not create state parent {}", bello.display()))?;
    }
    let bello_handle = open_path(&bello, false)?;
    validate_plain_file_object(&bello_handle, &bello)?;
    handles.push(bello_handle);

    let state = bello.join("SandboxState-v1");
    if !state.exists() {
        fs::create_dir(&state)
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
    format!("Local\\{profile_name}")
}

pub fn create_live_mutex(name: &str) -> Result<Handle> {
    Handle::new(
        unsafe { CreateMutexW(std::ptr::null(), 0, wide(name).as_ptr()) },
        "CreateMutexW",
    )
}

fn mutex_is_live(name: &str) -> bool {
    let handle = unsafe { OpenMutexW(SYNCHRONIZATION_SYNCHRONIZE, 0, wide(name).as_ptr()) };
    if handle == 0 {
        false
    } else {
        unsafe {
            CloseHandle(handle);
        }
        true
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
        if mutex_is_live(&data.mutex_name) {
            continue;
        }
        cleanup_data(&data)
            .with_context(|| format!("could not recover stale sandbox {}", data.profile_name))?;
        fs::remove_file(&path)
            .with_context(|| format!("could not remove recovered journal {}", path.display()))?;
    }
    Ok(())
}

fn validate_journal(data: &JournalData) -> Result<()> {
    if data.journal_version != JOURNAL_VERSION {
        return Err(anyhow!("unsupported recovery journal version"));
    }
    validate_profile_name(&data.profile_name)?;
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
    let _authority_handles = data
        .authorities
        .iter()
        .map(|record| reopen_recorded(record, false))
        .collect::<Result<Vec<_>>>()?;
    let sid = derive_profile_sid(&data.profile_name)?;
    let mut failures = Vec::new();
    let mut touched_handles = Vec::new();
    for record in data.touched_paths.iter().rev() {
        match reopen_recorded(record, true) {
            Ok(handle) => {
                if let Err(error) = acl::revoke(&handle, sid.0) {
                    failures.push(format!("revoke {}: {error:#}", record.path.display()));
                }
                touched_handles.push(handle);
            }
            Err(error) => failures.push(format!(
                "revalidate {} before revoke: {error:#}",
                record.path.display()
            )),
        }
    }
    if !failures.is_empty() {
        return Err(anyhow!(failures.join("; ")));
    }
    for record in &data.touched_paths {
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
    for created in data.created_paths.iter().rev() {
        match open_path(&created.path, false).and_then(|handle| file_identity(&handle)) {
            Ok(identity)
                if identity.volume_serial == created.identity.volume_serial
                    && identity.file_index == created.identity.file_index =>
            {
                if let Err(error) = fs::remove_dir(&created.path) {
                    // ERROR_DIR_NOT_EMPTY. Avoid ErrorKind::DirectoryNotEmpty,
                    // which postdates this crate's declared Rust 1.75 MSRV.
                    if error.raw_os_error() != Some(145) {
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
