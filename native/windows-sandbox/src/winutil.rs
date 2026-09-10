use anyhow::{anyhow, Context, Result};
use std::ffi::{c_void, OsStr, OsString};
use std::mem;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::os::windows::io::FromRawHandle;
use std::path::{Component, Path, PathBuf, Prefix};
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Globalization::CompareStringOrdinal;
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, GetFileInformationByHandle, GetFinalPathNameByHandleW,
    GetVolumeInformationByHandleW, BY_HANDLE_FILE_INFORMATION, FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_GENERIC_READ, FILE_SHARE_READ,
    FILE_SHARE_WRITE, OPEN_EXISTING, READ_CONTROL, WRITE_DAC,
};
use windows_sys::Win32::System::SystemServices::FILE_PERSISTENT_ACLS;

pub fn wide(value: impl AsRef<OsStr>) -> Vec<u16> {
    value.as_ref().encode_wide().chain(Some(0)).collect()
}

pub unsafe fn wide_ptr_to_os_string(mut ptr: *const u16) -> OsString {
    let start = ptr;
    while *ptr != 0 {
        ptr = ptr.add(1);
    }
    OsString::from_wide(std::slice::from_raw_parts(
        start,
        ptr.offset_from(start) as usize,
    ))
}

pub fn last_error(operation: &str) -> anyhow::Error {
    let code = unsafe { GetLastError() };
    anyhow!("{operation} failed with Win32 error {code}")
}

#[derive(Debug)]
pub struct Handle(pub HANDLE);

unsafe impl Send for Handle {}
unsafe impl Sync for Handle {}

impl Handle {
    pub fn new(raw: HANDLE, operation: &str) -> Result<Self> {
        if raw == 0 || raw == INVALID_HANDLE_VALUE {
            Err(last_error(operation))
        } else {
            Ok(Self(raw))
        }
    }

    pub fn raw(&self) -> HANDLE {
        self.0
    }

    pub fn into_raw(mut self) -> HANDLE {
        let raw = self.0;
        self.0 = 0;
        raw
    }
}

pub fn open_regular_file_read(path: &Path) -> Result<std::fs::File> {
    let handle = Handle::new(
        unsafe {
            CreateFileW(
                wide(path).as_ptr(),
                FILE_GENERIC_READ,
                FILE_SHARE_READ,
                std::ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT,
                0,
            )
        },
        &format!("CreateFileW(read {})", path.display()),
    )?;
    validate_final_path(&handle, path)?;
    validate_plain_file_object(&handle, path)?;
    let info = file_info(&handle)?;
    if info.dwFileAttributes & windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_DIRECTORY
        != 0
    {
        return Err(anyhow!("expected a regular file: {}", path.display()));
    }
    Ok(unsafe { std::fs::File::from_raw_handle(handle.into_raw() as *mut c_void) })
}

impl Drop for Handle {
    fn drop(&mut self) {
        if self.0 != 0 && self.0 != INVALID_HANDLE_VALUE {
            unsafe {
                CloseHandle(self.0);
            }
            self.0 = 0;
        }
    }
}

#[derive(Clone, Copy, Debug, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FileIdentity {
    pub volume_serial: u32,
    pub file_index: u64,
}

pub fn open_path(path: &Path, write_dac: bool) -> Result<Handle> {
    let access = READ_CONTROL | if write_dac { WRITE_DAC } else { 0 };
    let raw = unsafe {
        CreateFileW(
            wide(path).as_ptr(),
            access,
            // Deliberately omit FILE_SHARE_DELETE. Keeping authority handles
            // open prevents their names from being replaced while a command runs.
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            0,
        )
    };
    Handle::new(raw, &format!("CreateFileW({})", path.display()))
}

pub fn file_info(handle: &Handle) -> Result<BY_HANDLE_FILE_INFORMATION> {
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { mem::zeroed() };
    if unsafe { GetFileInformationByHandle(handle.raw(), &mut info) } == 0 {
        return Err(last_error("GetFileInformationByHandle"));
    }
    Ok(info)
}

pub fn file_identity(handle: &Handle) -> Result<FileIdentity> {
    let info = file_info(handle)?;
    Ok(FileIdentity {
        volume_serial: info.dwVolumeSerialNumber,
        file_index: ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64,
    })
}

pub fn validate_plain_file_object(handle: &Handle, path: &Path) -> Result<FileIdentity> {
    let info = file_info(handle)?;
    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(anyhow!(
            "reparse points are not accepted in a Windows sandbox authority: {}",
            path.display()
        ));
    }
    if info.nNumberOfLinks > 1 {
        return Err(anyhow!(
            "hard-linked objects are not accepted in a Windows sandbox authority: {}",
            path.display()
        ));
    }
    Ok(FileIdentity {
        volume_serial: info.dwVolumeSerialNumber,
        file_index: ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64,
    })
}

pub fn final_path(handle: &Handle) -> Result<PathBuf> {
    let needed = unsafe { GetFinalPathNameByHandleW(handle.raw(), std::ptr::null_mut(), 0, 0) };
    if needed == 0 {
        return Err(last_error("GetFinalPathNameByHandleW(size)"));
    }
    let mut buffer = vec![0_u16; needed as usize + 1];
    let written = unsafe {
        GetFinalPathNameByHandleW(handle.raw(), buffer.as_mut_ptr(), buffer.len() as u32, 0)
    };
    if written == 0 || written as usize >= buffer.len() {
        return Err(last_error("GetFinalPathNameByHandleW"));
    }
    buffer.truncate(written as usize);
    Ok(PathBuf::from(OsString::from_wide(&buffer)))
}

pub fn require_persistent_acls(handle: &Handle, path: &Path) -> Result<()> {
    let mut flags = 0_u32;
    if unsafe {
        GetVolumeInformationByHandleW(
            handle.raw(),
            std::ptr::null_mut(),
            0,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut flags,
            std::ptr::null_mut(),
            0,
        )
    } == 0
    {
        return Err(last_error(&format!(
            "GetVolumeInformationByHandleW({})",
            path.display()
        )));
    }
    if flags & FILE_PERSISTENT_ACLS == 0 {
        return Err(anyhow!(
            "the filesystem does not provide persistent ACLs: {}",
            path.display()
        ));
    }
    Ok(())
}

pub fn canonical_existing(path: &str, label: &str) -> Result<PathBuf> {
    if path.contains('\0') {
        return Err(anyhow!("{label} contains a NUL byte"));
    }
    let supplied = PathBuf::from(path);
    if !supplied.is_absolute() || !is_local_drive_path(&supplied) {
        return Err(anyhow!("{label} must be an absolute local drive path"));
    }
    let canonical = std::fs::canonicalize(&supplied)
        .with_context(|| format!("{label} does not resolve: {}", supplied.display()))?;
    if !is_local_drive_path(&canonical) {
        return Err(anyhow!("{label} resolved outside a local drive"));
    }
    Ok(canonical)
}

pub fn is_local_drive_path(path: &Path) -> bool {
    let Some(Component::Prefix(prefix)) = path.components().next() else {
        return false;
    };
    matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_))
}

pub fn is_normalized_local_absolute(path: &Path) -> bool {
    let raw: Vec<u16> = path.as_os_str().encode_wide().collect();
    if raw
        .split(|unit| *unit == b'\\' as u16 || *unit == b'/' as u16)
        .any(|segment| segment == [b'.' as u16] || segment == [b'.' as u16, b'.' as u16])
    {
        return false;
    }
    let mut components = path.components();
    let Some(Component::Prefix(prefix)) = components.next() else {
        return false;
    };
    if !matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_)) {
        return false;
    }
    if !matches!(components.next(), Some(Component::RootDir)) {
        return false;
    }
    components.all(|component| match component {
        Component::Normal(value) => {
            let units: Vec<u16> = value.encode_wide().collect();
            !units.is_empty()
                && !units.contains(&0)
                && !units.contains(&(b':' as u16))
                && !matches!(units.last(), Some(value) if *value == b'.' as u16 || *value == b' ' as u16)
        }
        _ => false,
    })
}

pub fn verbatim_local_absolute(path: &Path) -> Result<PathBuf> {
    if !is_normalized_local_absolute(path) {
        return Err(anyhow!(
            "path must be a normalized absolute local-drive path"
        ));
    }
    let Some(Component::Prefix(prefix)) = path.components().next() else {
        unreachable!("a normalized local path has a drive prefix");
    };
    let (drive, verbatim) = match prefix.kind() {
        Prefix::Disk(drive) => (drive, false),
        Prefix::VerbatimDisk(drive) => (drive, true),
        _ => unreachable!("a normalized local path has a drive prefix"),
    };
    // Authorities returned by canonicalize use extended-length drive paths.
    // Convert only strictly validated local paths to that spelling, without
    // resolving links or changing the supplied directory components.
    let mut value: Vec<u16> = path
        .as_os_str()
        .encode_wide()
        .map(|unit| {
            if unit == b'/' as u16 {
                b'\\' as u16
            } else {
                unit
            }
        })
        .collect();
    if !verbatim {
        value.splice(
            0..0,
            [b'\\' as u16, b'\\' as u16, b'?' as u16, b'\\' as u16],
        );
    }
    value[4] = drive.to_ascii_uppercase() as u16;
    Ok(PathBuf::from(OsString::from_wide(&value)))
}

pub fn contains(parent: &Path, child: &Path) -> bool {
    let parent = normalized_path_wide(parent);
    let child = normalized_path_wide(child);
    if child.len() < parent.len() || !ordinal_eq(&parent, &child[..parent.len()]) {
        return false;
    }
    child.len() == parent.len() || child.get(parent.len()) == Some(&(b'\\' as u16))
}

pub fn is_volume_root(path: &Path) -> bool {
    let mut components = path.components();
    let Some(Component::Prefix(prefix)) = components.next() else {
        return false;
    };
    if !matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_)) {
        return false;
    }
    matches!(components.next(), Some(Component::RootDir)) && components.next().is_none()
}

pub fn path_eq(parent: &Path, child: &Path) -> bool {
    let parent = normalized_path_wide(parent);
    let child = normalized_path_wide(child);
    ordinal_eq(&parent, &child)
}

fn normalized_path_wide(path: &Path) -> Vec<u16> {
    let mut value: Vec<u16> = path
        .as_os_str()
        .encode_wide()
        .map(|unit| {
            if unit == b'/' as u16 {
                b'\\' as u16
            } else {
                unit
            }
        })
        .collect();
    while value.last() == Some(&(b'\\' as u16)) {
        value.pop();
    }
    value
}

fn ordinal_eq(left: &[u16], right: &[u16]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    if left.is_empty() {
        return true;
    }
    // CSTR_EQUAL is 2. CompareStringOrdinal implements Windows' own ordinal,
    // case-insensitive path comparison instead of locale-sensitive lowercasing.
    unsafe {
        CompareStringOrdinal(
            left.as_ptr(),
            left.len() as i32,
            right.as_ptr(),
            right.len() as i32,
            1,
        ) == 2
    }
}

pub fn validate_final_path(handle: &Handle, expected: &Path) -> Result<()> {
    let actual = final_path(handle)?;
    if !path_eq(&actual, expected) {
        return Err(anyhow!(
            "path identity changed while establishing the sandbox: expected {}, opened {}",
            expected.display(),
            actual.display()
        ));
    }
    Ok(())
}

pub fn checked_usize_to_u32(value: usize, label: &str) -> Result<u32> {
    u32::try_from(value).with_context(|| format!("{label} is too large"))
}

pub fn as_void<T>(value: &T) -> *const c_void {
    value as *const T as *const c_void
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn private_drive_spellings_match_canonical_authorities() {
        let root = Path::new(r"\\?\C:\workspace");
        for supplied in [
            r"C:\workspace\.codex\bello-run",
            r"c:\workspace\.codex\bello-run",
            r"C:/workspace/.codex/bello-run",
            r"\\?\C:\workspace\.codex\bello-run",
            r"\\?\c:\workspace\.codex\bello-run",
        ] {
            let path = verbatim_local_absolute(Path::new(supplied)).unwrap();
            assert_eq!(path, root.join(r".codex\bello-run"));
            assert!(contains(root, &path));
            assert_eq!(
                path.strip_prefix(root).unwrap(),
                Path::new(r".codex\bello-run")
            );
        }
        for supplied in [
            r"C:\workspace-other\.supervisor",
            r"D:\workspace\.supervisor",
        ] {
            let path = verbatim_local_absolute(Path::new(supplied)).unwrap();
            assert!(!contains(root, &path));
        }
    }

    #[test]
    fn private_drive_spelling_does_not_accept_other_namespaces_or_ambiguity() {
        for supplied in [
            r"C:\workspace\..\outside",
            r"C:\workspace\.\private",
            r"C:\workspace\private.",
            r"C:\workspace\private ",
            r"C:\workspace\private:stream",
            r"C:workspace\private",
            r"\\server\share\private",
            r"\\?\UNC\server\share\private",
            r"\\.\C:\workspace\private",
        ] {
            assert!(
                verbatim_local_absolute(Path::new(supplied)).is_err(),
                "{supplied}"
            );
        }
    }

    #[test]
    fn private_lexical_paths_cannot_escape_with_parent_components() {
        assert!(is_normalized_local_absolute(Path::new(
            r"C:\workspace\.supervisor"
        )));
        assert!(!is_normalized_local_absolute(Path::new(
            r"C:\workspace\..\outside\private"
        )));
        assert!(!is_normalized_local_absolute(Path::new(
            r"C:\workspace\.\private"
        )));
        assert!(!is_normalized_local_absolute(Path::new(
            r"C:\workspace\private."
        )));
        assert!(!is_normalized_local_absolute(Path::new(
            r"\\server\share\private"
        )));
    }
}
