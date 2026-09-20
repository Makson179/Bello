//! Cooperating Bello helpers serialize only their ACL read/modify/write steps.
//! The persistent lock file lives in the already verified, owner-only state
//! directory. Never delete it on release: another waiter may hold its inode.

use crate::journal::StateDirectory;
use crate::winutil::{file_info, validate_final_path, validate_plain_file_object, wide, Handle};
use anyhow::{anyhow, Result};
use std::mem;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{GetLastError, ERROR_LOCK_VIOLATION};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, LockFileEx, UnlockFileEx, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_NORMAL,
    FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_GENERIC_READ,
    FILE_GENERIC_WRITE, FILE_SHARE_READ, FILE_SHARE_WRITE, LOCKFILE_EXCLUSIVE_LOCK,
    LOCKFILE_FAIL_IMMEDIATELY, OPEN_ALWAYS,
};
use windows_sys::Win32::System::IO::OVERLAPPED;

const LOCK_NAME: &str = "acl-mutations.lock";
// A busy lock is an error after this bound, never permission to mutate unlocked.
const WAIT_LIMIT: Duration = Duration::from_secs(60);
const WAIT_SLICE: Duration = Duration::from_millis(10);

#[derive(Debug)]
struct FileLock {
    handle: Handle,
}

/// Keep this guard only across the ACL read/modify/write/read-back operation,
/// not across a sandbox command. The borrow retains the verified ancestry pins.
pub struct AclMutationLock<'state> {
    _state: &'state StateDirectory,
    _lock: FileLock,
}

impl<'state> AclMutationLock<'state> {
    pub fn acquire(state: &'state StateDirectory, cancelled: &AtomicBool) -> Result<Self> {
        let lock = acquire_file(&state.path().join(LOCK_NAME), cancelled, WAIT_LIMIT)?;
        Ok(Self {
            _state: state,
            _lock: lock,
        })
    }
}

fn validate_lock_file(handle: &Handle, path: &Path) -> Result<()> {
    validate_plain_file_object(handle, path)?;
    validate_final_path(handle, path)?;
    if file_info(handle)?.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0 {
        return Err(anyhow!("the ACL mutation lock must be a regular file"));
    }
    Ok(())
}

fn acquire_file(path: &Path, cancelled: &AtomicBool, wait_limit: Duration) -> Result<FileLock> {
    if cancelled.load(Ordering::Acquire) {
        return Err(anyhow!("ACL mutation lock acquisition was cancelled"));
    }
    // Inherit the verified owner-only parent DACL. Do not reapply directory ACLs
    // here, follow a pre-planted link, inherit the handle, or allow replacement
    // while any holder/waiter has it open.
    let handle = Handle::new(
        unsafe {
            CreateFileW(
                wide(path).as_ptr(),
                FILE_GENERIC_READ | FILE_GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                std::ptr::null(),
                OPEN_ALWAYS,
                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
                0,
            )
        },
        "CreateFileW(ACL mutation lock)",
    )?;
    validate_lock_file(&handle, path)?;
    let start = Instant::now();
    loop {
        if cancelled.load(Ordering::Acquire) {
            return Err(anyhow!("ACL mutation lock acquisition was cancelled"));
        }
        let mut overlapped: OVERLAPPED = unsafe { mem::zeroed() };
        // Non-overlapped handle + FAIL_IMMEDIATELY never queues pending I/O.
        // Locking one byte beyond the empty file's EOF is supported by Windows.
        let acquired = unsafe {
            LockFileEx(
                handle.raw(),
                LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                0,
                1,
                0,
                &mut overlapped,
            )
        };
        if acquired != 0 {
            let lock = FileLock { handle };
            validate_lock_file(&lock.handle, path)?;
            if cancelled.load(Ordering::Acquire) {
                return Err(anyhow!("ACL mutation lock acquisition was cancelled"));
            }
            return Ok(lock);
        }
        let code = unsafe { GetLastError() };
        if code != ERROR_LOCK_VIOLATION {
            return Err(anyhow!(
                "LockFileEx(ACL mutation lock) failed with Win32 error {code}"
            ));
        }
        let elapsed = start.elapsed();
        if elapsed >= wait_limit {
            return Err(anyhow!(
                "ACL mutation lock is busy; timed out without changing permissions"
            ));
        }
        std::thread::sleep(WAIT_SLICE.min(wait_limit.saturating_sub(elapsed)));
    }
}

impl Drop for FileLock {
    fn drop(&mut self) {
        let mut overlapped: OVERLAPPED = unsafe { mem::zeroed() };
        unsafe {
            UnlockFileEx(self.handle.raw(), 0, 1, 0, &mut overlapped);
        }
        // Handle::drop closes the file even if explicit unlock failed. Windows
        // also releases this lock if the helper dies without running Drop.
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::acl;
    use std::fs;
    use std::path::PathBuf;
    use std::process::{Child, Command, Stdio};
    use std::sync::atomic::AtomicUsize;
    use std::sync::Arc;
    use std::time::{SystemTime, UNIX_EPOCH};

    struct Fixture(PathBuf);

    impl Fixture {
        fn new() -> Self {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let path =
                std::env::temp_dir().join(format!("bello-acl-lock-{}-{nonce}", std::process::id()));
            acl::create_state_directory(&path).unwrap();
            Self(fs::canonicalize(path).unwrap())
        }

        fn lock_path(&self) -> PathBuf {
            self.0.join(LOCK_NAME)
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.0).unwrap();
        }
    }

    #[test]
    fn public_acquire_honors_cancellation_before_creating_a_lock_file() {
        let state = crate::journal::state_directory().unwrap();
        let result = AclMutationLock::acquire(&state, &AtomicBool::new(true));
        assert!(result.err().unwrap().to_string().contains("cancelled"));
    }

    #[test]
    fn independent_handles_exclude_each_other_then_reacquire_after_drop() {
        let fixture = Fixture::new();
        let path = fixture.lock_path();
        let cancelled = AtomicBool::new(false);
        let first = acquire_file(&path, &cancelled, Duration::ZERO).unwrap();
        assert!(fs::rename(&path, fixture.0.join("moved-lock")).is_err());
        assert!(fs::remove_file(&path).is_err());
        let error = acquire_file(&path, &cancelled, Duration::from_millis(25)).unwrap_err();
        assert!(error.to_string().contains("busy"));
        drop(first);
        assert!(
            path.exists(),
            "release must not unlink a waiter's lock file"
        );
        let second = acquire_file(&path, &cancelled, Duration::ZERO).unwrap();
        drop(second);
    }

    #[test]
    fn cancellation_interrupts_a_contended_wait() {
        let fixture = Fixture::new();
        let path = fixture.lock_path();
        let first = acquire_file(&path, &AtomicBool::new(false), Duration::ZERO).unwrap();
        let cancelled = Arc::new(AtomicBool::new(false));
        let signal = Arc::clone(&cancelled);
        let notifier = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(30));
            signal.store(true, Ordering::Release);
        });
        let error = acquire_file(&path, &cancelled, Duration::from_secs(5)).unwrap_err();
        notifier.join().unwrap();
        assert!(error.to_string().contains("cancelled"));
        drop(first);
    }

    #[test]
    fn parallel_helpers_do_not_lose_read_modify_write_updates() {
        let fixture = Fixture::new();
        let value = Arc::new(AtomicUsize::new(0));
        let mut workers = Vec::new();
        for _ in 0..4 {
            let path = fixture.lock_path();
            let value = Arc::clone(&value);
            workers.push(std::thread::spawn(move || {
                for _ in 0..25 {
                    let _guard =
                        acquire_file(&path, &AtomicBool::new(false), Duration::from_secs(5))
                            .unwrap();
                    let previous = value.load(Ordering::Relaxed);
                    std::thread::yield_now();
                    value.store(previous + 1, Ordering::Relaxed);
                }
            }));
        }
        for worker in workers {
            worker.join().unwrap();
        }
        assert_eq!(value.load(Ordering::Relaxed), 100);
    }

    #[test]
    fn unsafe_existing_lock_leaf_is_rejected() {
        let fixture = Fixture::new();
        let path = fixture.lock_path();
        let target = fixture.0.join("other-file");
        fs::write(&target, b"unchanged").unwrap();
        fs::hard_link(&target, &path).unwrap();
        let error = acquire_file(&path, &AtomicBool::new(false), Duration::ZERO).unwrap_err();
        assert!(error.to_string().contains("hard-linked"));
        assert_eq!(fs::read(&target).unwrap(), b"unchanged");
        fs::remove_file(&path).unwrap();
        fs::create_dir(&path).unwrap();
        assert!(acquire_file(&path, &AtomicBool::new(false), Duration::ZERO).is_err());
    }

    #[test]
    fn junction_lock_leaf_is_rejected_without_touching_target() {
        let fixture = Fixture::new();
        let path = fixture.lock_path();
        let target = fixture.0.join("other-directory");
        fs::create_dir(&target).unwrap();
        fs::write(target.join("sentinel"), b"unchanged").unwrap();
        let linked = Command::new("cmd.exe")
            .args(["/d", "/s", "/c", "mklink", "/J"])
            .arg(&path)
            .arg(&target)
            .stdin(Stdio::null())
            .output()
            .unwrap();
        assert!(
            linked.status.success(),
            "could not create lock junction fixture"
        );
        assert!(acquire_file(&path, &AtomicBool::new(false), Duration::ZERO).is_err());
        assert_eq!(fs::read(target.join("sentinel")).unwrap(), b"unchanged");
        fs::remove_dir(&path).unwrap();
    }

    struct ChildGuard(Child);

    impl Drop for ChildGuard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    #[test]
    fn lock_child_process() {
        let Some(directory) = std::env::var_os("BELLO_ACL_LOCK_TEST_CHILD") else {
            return;
        };
        let directory = PathBuf::from(directory);
        let _guard = acquire_file(
            &directory.join(LOCK_NAME),
            &AtomicBool::new(false),
            Duration::from_secs(5),
        )
        .unwrap();
        fs::write(directory.join("ready"), b"locked").unwrap();
        std::thread::sleep(Duration::from_secs(30));
        panic!("parent did not terminate the lock-holding test child");
    }

    #[test]
    fn process_termination_releases_the_lock_without_drop() {
        let fixture = Fixture::new();
        let mut child = ChildGuard(
            Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "acl_lock::tests::lock_child_process",
                    "--nocapture",
                ])
                .env("BELLO_ACL_LOCK_TEST_CHILD", &fixture.0)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        let start = Instant::now();
        while !fixture.0.join("ready").exists() {
            assert!(
                child.0.try_wait().unwrap().is_none(),
                "lock child exited early"
            );
            assert!(
                start.elapsed() < Duration::from_secs(10),
                "lock child not ready"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        let path = fixture.lock_path();
        let error = acquire_file(&path, &AtomicBool::new(false), Duration::ZERO).unwrap_err();
        assert!(error.to_string().contains("busy"));
        child.0.kill().unwrap();
        child.0.wait().unwrap();
        let _guard = acquire_file(&path, &AtomicBool::new(false), Duration::from_secs(5)).unwrap();
    }
}
