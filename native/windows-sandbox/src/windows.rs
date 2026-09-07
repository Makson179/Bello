use crate::protocol::{Request, SandboxMode};
use crate::{acl, identity, journal, process, winutil};
use anyhow::{anyhow, Context, Result};
use identity::{create_profile, profile_local_app_data, random_profile_name, CapabilitySids};
use journal::{create_live_mutex, mutex_name, recover_stale, state_directory, Journal};
use process::{clean_environment, run_child, start_parent_monitor, Job};
use std::collections::VecDeque;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_DIRECTORY;
use windows_sys::Win32::System::Com::CoTaskMemFree;
use windows_sys::Win32::UI::Shell::{FOLDERID_Profile, SHGetKnownFolderPath};
use winutil::{
    canonical_existing, contains, file_identity, is_normalized_local_absolute, is_volume_root,
    open_path, path_eq, require_persistent_acls, validate_final_path, validate_plain_file_object,
    Handle,
};

const MAX_AUTHORITY_OBJECTS: usize = 500_000;

pub fn execute(request: Request) -> Result<i32> {
    let state_dir = state_directory()?;
    match request {
        Request::Recover { .. } => {
            recover_stale(state_dir.path())?;
            Ok(0)
        }
        Request::Run {
            command,
            cwd,
            root,
            mode,
            readable_roots,
            private_paths,
            network_access,
            ..
        } => run(
            state_dir.path(),
            command,
            cwd,
            root,
            mode,
            readable_roots,
            private_paths,
            network_access,
        ),
    }
}

#[allow(clippy::too_many_arguments)]
fn run(
    state_dir: &Path,
    command: String,
    cwd: String,
    root: String,
    mode: SandboxMode,
    readable_roots: Vec<String>,
    private_paths: Vec<String>,
    network_access: bool,
) -> Result<i32> {
    if command.trim().is_empty() || command.contains('\0') {
        return Err(anyhow!("command must be non-empty and contain no NUL byte"));
    }
    let root = canonical_existing(&root, "root")?;
    if !root.is_dir() || is_volume_root(&root) {
        return Err(anyhow!("root must be a non-volume directory"));
    }
    let readable_roots = readable_roots
        .iter()
        .enumerate()
        .map(|(index, value)| canonical_existing(value, &format!("readableRoots[{index}]")))
        .collect::<Result<Vec<_>>>()?;
    if let Some(volume) = readable_roots.iter().find(|path| is_volume_root(path)) {
        return Err(anyhow!(
            "readable roots cannot grant an entire volume: {}",
            volume.display()
        ));
    }
    validate_authority_relationships(&root, &readable_roots, mode)?;
    let cwd = canonical_existing(&cwd, "cwd")?;
    if !cwd.is_dir()
        || !std::iter::once(&root)
            .chain(readable_roots.iter())
            .filter(|path| path.is_dir())
            .any(|authority| contains(authority, &cwd))
    {
        return Err(anyhow!("cwd is outside the assigned directory authorities"));
    }
    if std::iter::once(&root)
        .chain(readable_roots.iter())
        .any(|authority| contains(authority, state_dir) || contains(state_dir, authority))
    {
        return Err(anyhow!(
            "the sandbox recovery directory overlaps a command authority"
        ));
    }
    // Never interpret recovery data until the requested authorities are known
    // not to overlap the fixed, OS-resolved state directory.
    recover_stale(state_dir)?;

    let authority_paths: Vec<PathBuf> = std::iter::once(root.clone())
        .chain(readable_roots.iter().cloned())
        .collect();
    let profile_name = random_profile_name()?;
    let mutex_name = mutex_name(&profile_name);
    let _live_mutex = create_live_mutex(&mutex_name)?;
    let mut journal = Journal::create(state_dir, &profile_name, &mutex_name, &authority_paths)?;
    let mut capabilities = CapabilitySids::for_network(network_access)?;
    let sid = create_profile(journal.profile_name(), &capabilities)?;
    let profile_local = profile_local_app_data(sid.0)?;
    let job = Job::create()?;
    let cancelled = Arc::new(AtomicBool::new(false));
    start_parent_monitor(Arc::clone(&job), Arc::clone(&cancelled))?;

    let operation = (|| -> Result<i32> {
        let private_paths =
            prepare_private_paths(&root, &readable_roots, mode, private_paths, &mut journal)?;
        let authorities: Vec<&PathBuf> = std::iter::once(&root)
            .chain(readable_roots.iter())
            .collect();
        for authority in &authorities {
            scan_authority(authority, &cancelled)?;
        }
        if cancelled.load(Ordering::Acquire) {
            return Err(anyhow!("controller closed stdin during sandbox setup"));
        }

        // Retain only authority/private root handles. The traversal opens each
        // descendant briefly, so model commands remain free to rename/delete
        // ordinary descendants after launch.
        let mut authority_handles: Vec<(PathBuf, Handle)> = Vec::new();
        for authority in &authorities {
            let handle = open_path(authority, true)?;
            validate_final_path(&handle, authority)?;
            require_persistent_acls(&handle, authority)?;
            journal.before_acl_mutation(authority, &handle)?;
            let authority_mode = if path_eq(authority, &root) {
                mode
            } else {
                SandboxMode::ReadOnly
            };
            acl::grant(&handle, sid.0, authority_mode)
                .with_context(|| format!("could not grant {}", authority.display()))?;
            authority_handles.push(((*authority).clone(), handle));
            if cancelled.load(Ordering::Acquire) {
                return Err(anyhow!("controller closed stdin during ACL setup"));
            }
        }

        let mut private_handles: Vec<(PathBuf, Handle)> = Vec::new();
        for private in &private_paths {
            for ancestor in private_ancestors(&authorities, private)? {
                if private_handles
                    .iter()
                    .any(|(held, _)| path_eq(held, &ancestor))
                {
                    continue;
                }
                let handle = open_path(&ancestor, false)?;
                validate_final_path(&handle, &ancestor)?;
                validate_plain_file_object(&handle, &ancestor)?;
                private_handles.push((ancestor, handle));
            }
            let handle = open_path(private, true)?;
            validate_final_path(&handle, private)?;
            validate_plain_file_object(&handle, private)?;
            journal.before_acl_mutation(private, &handle)?;
            acl::deny_all(&handle, sid.0)
                .with_context(|| format!("could not protect {}", private.display()))?;
            private_handles.push((private.clone(), handle));
        }
        verify_effective_tree(&authorities, &private_paths, sid.0, mode, &cancelled)?;
        if cancelled.load(Ordering::Acquire) {
            return Err(anyhow!("controller closed stdin before process launch"));
        }

        let mut environment = clean_environment(&profile_local, &root, &readable_roots)?;
        run_child(
            &command,
            &cwd,
            sid.0,
            &mut capabilities,
            &mut environment,
            &job,
            &cancelled,
        )
    })();

    let requested_exit = operation.as_ref().copied().unwrap_or(125) as u32;
    let _ = job.terminate(requested_exit);
    let empty = job.ensure_empty();
    let mut failures = job.failure_messages();
    if let Err(error) = &empty {
        failures.push(format!("could not prove job empty: {error:#}"));
    }
    let cleanup = if empty.is_ok() {
        journal.cleanup()
    } else {
        journal.defer_cleanup();
        Err(anyhow!(
            "ACL/profile cleanup deferred to crash recovery because the job is not proven empty"
        ))
    };
    if let Err(error) = cleanup {
        failures.push(format!("sandbox ACL/profile cleanup failed: {error:#}"));
    }
    if let Err(error) = operation {
        failures.insert(0, format!("sandbox command failed: {error:#}"));
    }
    failures.sort();
    failures.dedup();
    if failures.is_empty() {
        Ok(requested_exit as i32)
    } else {
        Err(anyhow!(failures.join("; ")))
    }
}

fn private_ancestors(authorities: &[&PathBuf], private: &Path) -> Result<Vec<PathBuf>> {
    let authority = authorities
        .iter()
        .find(|authority| contains(authority, private))
        .ok_or_else(|| anyhow!("private path is outside every command authority"))?;
    let mut ancestors = Vec::new();
    let mut current = private.parent();
    while let Some(path) = current {
        if path_eq(path, authority) {
            ancestors.reverse();
            return Ok(ancestors);
        }
        if !contains(authority, path) {
            break;
        }
        ancestors.push(path.to_owned());
        current = path.parent();
    }
    Err(anyhow!(
        "could not establish private-path ancestors for {}",
        private.display()
    ))
}

fn validate_authority_relationships(
    root: &Path,
    readable_roots: &[PathBuf],
    mode: SandboxMode,
) -> Result<()> {
    let home = profile_directory()?;
    if contains(root, &home) {
        return Err(anyhow!(
            "the writable/read-only root cannot contain the account home"
        ));
    }
    for (index, authority) in readable_roots.iter().enumerate() {
        if path_eq(root, authority) {
            return Err(anyhow!("readableRoots[{index}] duplicates root"));
        }
        if mode == SandboxMode::WorkspaceWrite
            && (contains(root, authority) || contains(authority, root))
        {
            return Err(anyhow!(
                "a read-only authority overlaps the writable root: {}",
                authority.display()
            ));
        }
        if contains(authority, &home) {
            return Err(anyhow!(
                "a readable dependency cannot contain the account home: {}",
                authority.display()
            ));
        }
        for earlier in &readable_roots[..index] {
            if path_eq(earlier, authority) {
                return Err(anyhow!("duplicate readable root: {}", authority.display()));
            }
        }
    }
    Ok(())
}

fn profile_directory() -> Result<PathBuf> {
    let mut raw: *mut u16 = std::ptr::null_mut();
    let hr = unsafe { SHGetKnownFolderPath(&FOLDERID_Profile, 0, 0, &mut raw) };
    if hr < 0 || raw.is_null() {
        return Err(anyhow!(
            "SHGetKnownFolderPath(Profile) failed with HRESULT 0x{:08x}",
            hr as u32
        ));
    }
    let path = PathBuf::from(unsafe { winutil::wide_ptr_to_os_string(raw) });
    unsafe {
        CoTaskMemFree(raw as *const std::ffi::c_void);
    }
    if !is_normalized_local_absolute(&path) {
        return Err(anyhow!("the account profile is not on a local drive"));
    }
    fs::canonicalize(&path)
        .with_context(|| format!("could not canonicalize account profile {}", path.display()))
}

fn scan_authority(root: &Path, cancelled: &Arc<AtomicBool>) -> Result<()> {
    let mut pending = VecDeque::from([root.to_owned()]);
    let mut visited = 0_usize;
    while let Some(path) = pending.pop_front() {
        if cancelled.load(Ordering::Acquire) {
            return Err(anyhow!(
                "controller closed stdin while validating filesystem"
            ));
        }
        visited += 1;
        if visited > MAX_AUTHORITY_OBJECTS {
            return Err(anyhow!(
                "sandbox authority exceeds the {MAX_AUTHORITY_OBJECTS}-object validation limit"
            ));
        }
        let handle = open_path(&path, false)?;
        validate_final_path(&handle, &std::fs::canonicalize(&path)?)?;
        let info = validate_plain_file_object(&handle, &path)?;
        acl::require_non_null_dacl(&handle)
            .with_context(|| format!("unsupported DACL on {}", path.display()))?;
        if info.file_index == 0 {
            return Err(anyhow!(
                "filesystem returned no stable file identity for {}",
                path.display()
            ));
        }
        let raw = winutil::file_info(&handle)?;
        drop(handle);
        if raw.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0 {
            for entry in fs::read_dir(&path)
                .with_context(|| format!("could not enumerate {}", path.display()))?
            {
                pending.push_back(entry?.path());
            }
        }
    }
    Ok(())
}

fn prepare_private_paths(
    root: &Path,
    readable_roots: &[PathBuf],
    mode: SandboxMode,
    supplied: Vec<String>,
    journal: &mut Journal,
) -> Result<Vec<PathBuf>> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    for (index, value) in supplied.iter().enumerate() {
        let path = PathBuf::from(value);
        if !is_normalized_local_absolute(&path) {
            return Err(anyhow!(
                "privatePaths[{index}] must be a normalized absolute local-drive path"
            ));
        }
        let authority = std::iter::once(root)
            .chain(readable_roots.iter().map(PathBuf::as_path))
            .find(|authority| contains(authority, &path))
            .ok_or_else(|| anyhow!("privatePaths[{index}] is outside every authority"))?;
        if path.exists() {
            let canonical = std::fs::canonicalize(&path)?;
            if !contains(authority, &canonical) {
                return Err(anyhow!("private path resolves outside its authority"));
            }
            if !candidates
                .iter()
                .any(|existing| path_eq(existing, &canonical))
            {
                candidates.push(canonical);
            }
        } else if mode == SandboxMode::WorkspaceWrite && path_eq(authority, root) {
            validate_nearest_existing_ancestor(authority, &path)?;
            materialize_private(root, &path, journal)?;
            let canonical = std::fs::canonicalize(&path)?;
            candidates.push(canonical);
        }
    }
    // These names are part of Bello's hard security contract even if an older
    // Python caller omitted them. Materialize missing names only in a writable
    // root; a read-only root cannot create them after launch.
    for relative in [
        PathBuf::from(".supervisor"),
        PathBuf::from(".codex").join("bello-run"),
    ] {
        let path = root.join(&relative);
        if path.exists() {
            let canonical = std::fs::canonicalize(&path)?;
            if !candidates
                .iter()
                .any(|existing| path_eq(existing, &canonical))
            {
                candidates.push(canonical);
            }
        } else if mode == SandboxMode::WorkspaceWrite {
            materialize_private(root, &path, journal)?;
            candidates.push(std::fs::canonicalize(&path)?);
        }
    }
    Ok(candidates)
}

fn validate_nearest_existing_ancestor(authority: &Path, target: &Path) -> Result<()> {
    let mut nearest = target;
    while !nearest.exists() {
        nearest = nearest
            .parent()
            .ok_or_else(|| anyhow!("private path has no existing ancestor"))?;
    }
    let canonical = fs::canonicalize(nearest).with_context(|| {
        format!(
            "could not canonicalize private path ancestor {}",
            nearest.display()
        )
    })?;
    if !contains(authority, &canonical) {
        return Err(anyhow!(
            "private path ancestor resolves outside its authority: {}",
            target.display()
        ));
    }
    Ok(())
}

fn materialize_private(root: &Path, target: &Path, journal: &mut Journal) -> Result<()> {
    if !contains(root, target) || path_eq(root, target) {
        return Err(anyhow!("invalid private path {}", target.display()));
    }
    let relative = target
        .strip_prefix(root)
        .map_err(|_| anyhow!("private path casing does not match canonical root"))?;
    let mut current = root.to_owned();
    for component in relative.components() {
        current.push(component);
        if current.exists() {
            continue;
        }
        fs::create_dir(&current)
            .with_context(|| format!("could not materialize private path {}", current.display()))?;
        let handle = open_path(&current, false)?;
        let identity = file_identity(&handle)?;
        journal.record_created(current.clone(), identity)?;
    }
    Ok(())
}

fn verify_effective_tree(
    authorities: &[&PathBuf],
    private_paths: &[PathBuf],
    sid: *mut std::ffi::c_void,
    root_mode: SandboxMode,
    cancelled: &Arc<AtomicBool>,
) -> Result<()> {
    // ACL propagation can skip a locked/protected child without making the
    // root SetSecurityInfo call fail. Re-walk and verify the exact unique SID
    // ACE on every reachable object before any untrusted process starts.
    for authority in authorities {
        let authority_mode = if path_eq(authority, authorities[0]) {
            root_mode
        } else {
            SandboxMode::ReadOnly
        };
        acl::verify_tree(authority, private_paths, sid, authority_mode, cancelled)?;
    }
    Ok(())
}
